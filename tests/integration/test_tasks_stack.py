"""The stack tier of `taskcoverage.py`, run against a testcontainers Infrahub.

Every method runs a record's invocations and then asserts its postcondition. An
exit code is not one: `demo-clean --branch does-not-exist` exits zero and does
nothing.

**Everything lives in one class, and the methods run in definition order.** The
`infrahub_testcontainers` fixtures are all `scope="class"`, so a second class is
a second stack, and each method here depends on what the ones above it left.
`pytest -k` on a single method deselects the ones that load the data, so run the
whole class or nothing.

**The first method is the wrong-stack guard, and it has to stay first.** A
developer's `.env` points at another Infrahub on port 8000. If the override in
`task_environment` ever fails to take, every task below would run against that
server, mutate a demo somebody is presenting from, and quite possibly pass.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import tasks
import yaml
from infrahub_sdk.testing.docker import TestInfrahubDockerClient
from infrahub_testcontainers.container import InfrahubDockerCompose

from .conftest import REPO_ROOT, run_task, task_output
from .stack import relax_healthcheck_budgets
from .taskcoverage import records_by_task
from .test_infrahub import MANIFEST, OPTICAL_ELEMENT_KINDS, TOKEN

RECORDS = records_by_task()

WRONG_ADDRESS = "http://localhost:8000"
"""Where a developer's `.env` points. No task in this module may reach it."""

SURFACE_BRANCH = "task-surface"
"""Created, listed and deleted by the three branch tasks."""
LOADING_BRANCH = "task-loading"
"""What the four loading tasks build up, one file at a time."""
PROVISION_BRANCH = "task-provision"
"""So `demo-provision --service` proves itself without disturbing `demo`."""

MENU_FILE = REPO_ROOT / "menus" / "otn.yml"

SCHEMA_CONVERGENCE_TIMEOUT = 180
"""How long the schema summary may take to catch up with a load, in seconds.

A ceiling, not a target. `infrahubctl schema load` returns before both API
servers report the new kinds, and a single read straight after it comes back a
kind or two short on a loaded host.
"""

ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;]*[a-zA-Z]")

pytestmark = pytest.mark.integration


SOURCE_LOCATION = re.compile(r"\S+\.py:\d+")
"""The column `rich` writes the caller's file and line into.

It lands inside the sentence rather than beside it, so a verdict arrives as
`monitor_completeness::MonitorCompletenessCheck: check.py:112 FAILED` and
`COLUMNS` does not move it. No postcondition in this module expects such a
token, so dropping it is safe here and only here: it would corrupt a JSON
string value, which is why `json_payload` below does not do it.
"""


def plain(output: str) -> str:
    """One line of terminal output with the colour, the wrapping and rich's
    source-location column taken out.

    `infrahubctl` writes through `rich`, which wraps a check verdict across
    lines and hyperlinks the file it came from, so a substring assertion has to
    read the text rather than the bytes.
    """
    return " ".join(SOURCE_LOCATION.sub("", ANSI.sub("", output)).split())


def json_payload(output: str) -> Any:
    """The JSON document a transform printed, out of the task output around it.

    A task runs `infrahubctl` on a pty, which colours the output and wraps a
    long string value across lines. So the colour comes off first, and the
    decoder is not strict: a wrapped sentence carries a newline the writer
    never put there. Every figure a postcondition reads is a number or a short
    name, neither of which the wrapping reaches.
    """
    lines = ANSI.sub("", output).splitlines()
    start = next((index for index, line in enumerate(lines) if line.strip() == "{"), None)
    assert start is not None, f"no JSON document in the output:\n{output[-2000:]}"
    payload, _ = json.JSONDecoder(strict=False).raw_decode("\n".join(lines[start:]))
    return payload


def menu_item_count() -> int:
    """Every entry in `menus/otn.yml`, children included."""

    def walk(items: list[dict[str, Any]]) -> int:
        total = 0
        for item in items:
            total += 1
            children = item.get("children") or {}
            total += walk(children.get("data") or [])
        return total

    return walk(yaml.safe_load(MENU_FILE.read_text())["spec"]["data"])


def schema_kinds(schemas: list[dict[str, Any]]) -> set[str]:
    """Every kind `schemas/` defines, as the schema summary names them."""
    return {
        f"{entry['namespace']}{entry['name']}"
        for document in schemas
        for key in ("nodes", "generics")
        for entry in document.get(key) or []
    }


class TestTasksAgainstAStack(TestInfrahubDockerClient):
    """One Infrahub, built up by the tasks under test in the order below."""

    @pytest.fixture(scope="class")
    @classmethod
    def infrahub_compose(
        cls,
        tmp_directory: Path,
        remote_repos_dir: Path,  # noqa: ARG002 - ordering: the bind mount must exist before compose runs
        remote_backups_dir: Path,  # noqa: ARG002 - same
        infrahub_version: str,
        deployment_type: str | None,
    ) -> InfrahubDockerCompose:
        """The stack, with room to start slowly. Mirrors `test_infrahub.py`."""
        compose = InfrahubDockerCompose.init(
            directory=tmp_directory,
            version=infrahub_version,
            deployment_type=deployment_type,
        )
        relax_healthcheck_budgets(tmp_directory / "docker-compose.yml")
        return compose

    # ----------------------------------------------------------------- #
    # Reads, so a postcondition can look at what a task did
    # ----------------------------------------------------------------- #

    @staticmethod
    def query(address: str, document: str, branch: str = "main") -> dict[str, Any]:
        """Run a GraphQL document and fail on `errors`, whatever the status code."""
        response = httpx.post(
            f"{address}/graphql/{branch}",
            json={"query": document},
            headers={"X-INFRAHUB-KEY": TOKEN},
            timeout=180.0,
        )
        payload = response.json()
        assert "errors" not in payload, f"GraphQL errors (HTTP {response.status_code}): {payload['errors']}"
        data: dict[str, Any] = payload["data"]
        return data

    @classmethod
    def count(cls, address: str, kind: str, branch: str = "main") -> int:
        return int(cls.query(address, f"{{ {kind} {{ count }} }}", branch)[kind]["count"])

    @classmethod
    def branches(cls, address: str) -> dict[str, bool]:
        """Every branch on the server, mapped to whether it is synced with Git."""
        data = cls.query(address, "{ Branch { name sync_with_git } }")
        return {entry["name"]: bool(entry["sync_with_git"]) for entry in data["Branch"]}

    @classmethod
    def service_status(cls, address: str, name: str, branch: str) -> str | None:
        """One service's status on a branch, or `None` when it is not there."""
        data = cls.query(
            address,
            f'{{ OtnService(name__value: "{name}") {{ edges {{ node {{ status {{ value }} }} }} }} }}',
            branch,
        )
        edges = data["OtnService"]["edges"]
        return str(edges[0]["node"]["status"]["value"]) if edges else None

    @staticmethod
    def summary_kinds(address: str, branch: str) -> set[str]:
        """Every kind the schema summary reports for a branch, generics included."""
        response = httpx.get(
            f"{address}/api/schema/summary",
            params={"branch": branch},
            headers={"X-INFRAHUB-KEY": TOKEN},
            timeout=60.0,
        )
        response.raise_for_status()
        summary = response.json()
        return set(summary.get("nodes", {})) | set(summary.get("generics", {}))

    # ----------------------------------------------------------------- #
    # The guard. Nothing below is trustworthy if this one fails.
    # ----------------------------------------------------------------- #

    def test_the_tasks_report_the_testcontainers_address_and_not_the_one_in_env(self, task_environment: str) -> None:
        """`info`: both forms name the stack this suite started.

        `task_environment` asserts the positive case on the way in. This asserts
        the negative one, which is the failure that would otherwise pass: a task
        that reached the developer's stack on port 8000 would answer every
        question below, correctly, about the wrong server.
        """
        assert task_environment != WRONG_ADDRESS, "the testcontainers stack took the port `.env` names"

        for arguments in RECORDS["info"].invocations:
            output = task_output(run_task("info", arguments), f"info {arguments}")
            assert task_environment in output
            assert WRONG_ADDRESS not in output

    # ----------------------------------------------------------------- #
    # Branches
    # ----------------------------------------------------------------- #

    def test_branch_create_makes_a_branch_that_is_synced_with_git(self, task_environment: str) -> None:
        """`branch-create`: the branch exists, and `sync_with_git` is on.

        The flag is the whole reason the task exists. `infrahubctl` defaults the
        other way, and a branch that is not synced runs the two built-in
        validators instead of this repository's checks.
        """
        task_output(run_task("branch-create", f"--name {SURFACE_BRANCH}"), "branch-create")

        branches = self.branches(task_environment)
        assert SURFACE_BRANCH in branches
        assert branches[SURFACE_BRANCH] is True

    def test_branch_list_names_the_branch_that_was_just_created(self, task_environment: str) -> None:  # noqa: ARG002
        """`branch-list`: the listing names the scratch branch."""
        output = task_output(run_task("branch-list"), "branch-list")
        assert SURFACE_BRANCH in plain(output)

    def test_branch_delete_removes_it_from_the_graph_and_from_the_listing(self, task_environment: str) -> None:
        """`branch-delete`: gone from the graph, and gone from `branch-list`."""
        task_output(run_task("branch-delete", f"--name {SURFACE_BRANCH}"), "branch-delete")

        assert SURFACE_BRANCH not in self.branches(task_environment)
        assert SURFACE_BRANCH not in plain(task_output(run_task("branch-list"), "branch-list"))

    # ----------------------------------------------------------------- #
    # Loading, one task at a time onto one branch
    # ----------------------------------------------------------------- #

    def test_load_schema_puts_every_kind_the_schemas_define_on_the_branch(
        self, task_environment: str, schemas: list[dict[str, Any]]
    ) -> None:
        """`load-schema`: the branch's summary lists every kind `schemas/` defines.

        Polled, because the load returning and the summary reporting the kinds
        are two events. Read as one, this reports a schema that is merely late
        as a schema the task never loaded.
        """
        task_output(run_task("branch-create", f"--name {LOADING_BRANCH}"), "branch-create")
        task_output(run_task("load-schema", f"--branch {LOADING_BRANCH}"), "load-schema")

        defined = schema_kinds(schemas)
        assert defined
        deadline = time.monotonic() + SCHEMA_CONVERGENCE_TIMEOUT
        missing = defined
        while missing and time.monotonic() < deadline:
            missing = defined - self.summary_kinds(task_environment, LOADING_BRANCH)
            if missing:
                time.sleep(5)
        assert not missing, f"kinds `schemas/` defines that {LOADING_BRANCH} does not report: {sorted(missing)}"

    def test_load_menu_puts_one_item_per_entry_on_the_branch(self, task_environment: str) -> None:
        """`load-menu`: one `CoreMenuItem` in the `Otn` namespace per entry in the file."""
        task_output(run_task("load-menu", f"--branch {LOADING_BRANCH}"), "load-menu")

        data = self.query(
            task_environment,
            '{ CoreMenuItem(namespace__value: "Otn") { count } }',
            LOADING_BRANCH,
        )
        assert data["CoreMenuItem"]["count"] == menu_item_count()

    def test_load_objects_with_a_file_loads_only_that_file(self, task_environment: str) -> None:
        """`load-objects --file`: the client signals arrive and nothing else does."""
        task_output(
            run_task("load-objects", f"--branch {LOADING_BRANCH} --file objects/04_client_signals.yml"),
            "load-objects --file",
        )

        assert self.count(task_environment, "OtnClientSignal", LOADING_BRANCH) == 11
        assert self.count(task_environment, "OtnSite", LOADING_BRANCH) == 0

    def test_load_objects_with_the_directory_loads_the_manifest_total(self, task_environment: str) -> None:
        """`load-objects`: every generated kind reaches the count the manifest declares."""
        task_output(run_task("load-objects", f"--branch {LOADING_BRANCH}"), "load-objects")

        document = "{" + " ".join(f"{kind}: {kind} {{ count }}" for kind in sorted(MANIFEST)) + "}"
        data = self.query(task_environment, document, LOADING_BRANCH)
        assert {kind: data[kind]["count"] for kind in MANIFEST} == MANIFEST

    def test_load_puts_the_schema_the_menu_and_the_dataset_on_main(self, task_environment: str) -> None:
        """`load`: `main` holds the manifest's site and carrier counts.

        Onto `main` rather than onto a fourth branch, because every scenario
        below forks from `main` and a loaded one saves each of them a minute.
        """
        task_output(run_task("load", "--branch main"), "load")

        assert self.count(task_environment, "OtnSite") == MANIFEST["OtnSite"]
        assert self.count(task_environment, "OtnOpticalCarrier") == MANIFEST["OtnOpticalCarrier"]

    def test_load_repository_needs_a_mount_this_stack_does_not_have(self) -> None:
        """`load-repository`: not runnable here, and skipped rather than faked.

        The task checks `/remote` inside `infrahub-demo-otn-infrahub-server-1`,
        which is the developer's compose project rather than this stack, and the
        testcontainers stack bind-mounts no export for it to clone.
        `test_infrahub.py::test_load_repository` registers this repository here
        through the SDK instead.
        """
        pytest.skip("the testcontainers stack has no /remote bind mount for `load-repository` to publish into")

    # ----------------------------------------------------------------- #
    # Reading the loaded data
    # ----------------------------------------------------------------- #

    def test_inventory_reports_the_figures_the_manifest_declares(self, task_environment: str) -> None:  # noqa: ARG002
        """`inventory`: the printed element and carrier counts come from the manifest."""
        output = plain(task_output(run_task("inventory", "--branch main"), "inventory"))

        elements = sum(MANIFEST[kind] for kind in OPTICAL_ELEMENT_KINDS)
        assert f"{elements} optical elements" in output
        assert f"{MANIFEST['OtnOpticalCarrier']} carriers" in output

    def test_check_with_a_name_runs_that_check_and_no_other(self, task_environment: str) -> None:  # noqa: ARG002
        """`check --name`: one check reports a verdict and the other eight do not."""
        output = plain(task_output(run_task("check", "--name units_import"), "check --name units_import"))

        assert "units_import::UnitsImportCheck" in output
        for name in tasks.CHECKS:
            if name != "units_import":
                assert f"{name}::" not in output

    def test_check_without_a_name_runs_all_nine(self, task_environment: str) -> None:  # noqa: ARG002
        """`check`: every name in `CHECKS` reports a verdict."""
        output = plain(task_output(run_task("check"), "check"))

        missing = [name for name in tasks.CHECKS if f"{name}::" not in output]
        assert not missing, f"checks that reported no verdict: {missing}"

    # ----------------------------------------------------------------- #
    # The walkthrough
    # ----------------------------------------------------------------- #

    def test_demo_setup_prepares_the_demo_branch(self, task_environment: str) -> None:
        """`demo-setup`: the branch holds the dataset, the five requests and the target group."""
        task_output(run_task("demo-setup"), "demo-setup")

        assert tasks.DEMO_BRANCH in self.branches(task_environment)
        assert self.count(task_environment, "OtnSite", tasks.DEMO_BRANCH) == MANIFEST["OtnSite"]
        assert self.count(task_environment, "OtnService", tasks.DEMO_BRANCH) == len(tasks.DEMO_SERVICES)

        group = self.query(
            task_environment,
            f'{{ CoreGeneratorGroup(name__value: "{tasks.GENERATOR_GROUP}") {{ count }} }}',
            tasks.DEMO_BRANCH,
        )
        assert group["CoreGeneratorGroup"]["count"] == 1

    def test_demo_provision_provisions_the_service_it_is_given_and_no_other(self, task_environment: str) -> None:
        """`demo-provision --service`: that one service goes active, the other four stay planned.

        On a branch of its own, because the walkthrough refuses this same
        service later and a service already provisioned would change what
        `demo-refusal` has to say.
        """
        wanted = "svc-fra-mil-ai-400g"
        task_output(
            run_task("demo-provision", f"--branch {PROVISION_BRANCH} --service {wanted}"),
            "demo-provision",
        )

        assert self.service_status(task_environment, wanted, PROVISION_BRANCH) == "active"
        for name in tasks.DEMO_SERVICES:
            if name != wanted:
                assert self.service_status(task_environment, name, PROVISION_BRANCH) == "planned"

    def test_demo_budget_names_the_worst_margin_and_the_sections_it_crosses(self, task_environment: str) -> None:  # noqa: ARG002
        """`demo-budget`: every carrier is budgeted, worst margin first, sections named."""
        report = json_payload(task_output(run_task("demo-budget"), "demo-budget"))

        assert report["carrier_count"] == MANIFEST["OtnOpticalCarrier"]
        margins = [carrier["verdict"]["osnr_margin_mdb"] for carrier in report["carriers"]]
        assert margins == sorted(margins), "the report is meant to lead with the worst margin"
        assert report["carriers"][0]["sections"], "the worst carrier names no section"

    def test_demo_drift_lists_the_seeded_droop(self, task_environment: str) -> None:  # noqa: ARG002
        """`demo-drift`: every amplifier and pump is compared, and the seeded droop is found."""
        report = json_payload(task_output(run_task("demo-drift"), "demo-drift"))

        assert report["compared"] == MANIFEST["OtnAmplifier"] + MANIFEST["OtnRamanPump"]
        assert report["beyond_tolerance"] > 0
        assert any(stage["kind"] == "amplifier" for stage in report["stages"])
        assert report["worst_shortfall_db"] > 0

    # ----------------------------------------------------------------- #
    # The loadable scenarios, cheapest first
    # ----------------------------------------------------------------- #

    def test_demo_raman_puts_the_pumps_on_its_branch_and_the_check_turns_green(self, task_environment: str) -> None:
        """`demo-raman`: six pumps on `raman-par-mad`, and `osnr_margin` passes there."""
        output = plain(task_output(run_task("demo-raman"), "demo-raman"))

        pumps = self.count(task_environment, "OtnRamanPump", tasks.RAMAN_BRANCH)
        assert pumps == MANIFEST["OtnRamanPump"] + 6
        assert "osnr_margin::OsnrMarginCheck: PASSED" in output

    def test_demo_monitor_gap_finds_the_amplifier_nobody_can_measure(self, task_environment: str) -> None:
        """`demo-monitor-gap`: the extra amplifier is on the branch and the check names it."""
        output = plain(task_output(run_task("demo-monitor-gap"), "demo-monitor-gap"))

        amplifiers = self.count(task_environment, "OtnAmplifier", tasks.MONITOR_GAP_BRANCH)
        assert amplifiers == MANIFEST["OtnAmplifier"] + 1
        assert "monitor_completeness::MonitorCompletenessCheck: FAILED" in output
        assert "amp-ham-ber-11" in output

    def test_demo_diversity_provisions_both_pairs_and_names_the_shared_duct(self, task_environment: str) -> None:
        """`demo-diversity`: all four feeds provision, and the check names the shared duct."""
        output = plain(task_output(run_task("demo-diversity"), "demo-diversity"))

        for name in tasks.DIVERSITY_SERVICES:
            assert self.service_status(task_environment, name, tasks.DIVERSITY_BRANCH) == "active"
        assert "diversity::DiversityCheck: FAILED" in output
        assert "cd-fra-north" in output

    def test_demo_odu_grooms_ten_circuits_and_refuses_the_eleventh(self, task_environment: str) -> None:
        """`demo-odu`: both files land, ten circuits groom, the eleventh is refused.

        The check passes, and that is its finding: a container filled to its
        capacity is not an overfilled one, so nothing here blocks a merge.
        """
        output = plain(task_output(run_task("demo-odu"), "demo-odu"))

        data = self.query(
            task_environment,
            """{
              ten_in_one: OtnOpticalCarrier(name__value: "oc-ch094-lon-mil") { count }
              mixed_fill: OtnContainer(name__value: "odu-mix-ch091-fra-vie-100g") { count }
            }""",
            tasks.ODU_BRANCH,
        )
        assert data["ten_in_one"]["count"] == 1
        assert data["mixed_fill"]["count"] == 1

        for name in tasks.ODU_SERVICES[:-1]:
            assert self.service_status(task_environment, name, tasks.ODU_BRANCH) == "active"
        assert self.service_status(task_environment, tasks.ODU_SERVICES[-1], tasks.ODU_BRANCH) == "rejected"
        assert "container_capacity::ContainerCapacityCheck: PASSED" in output

    def test_demo_regenerator_refuses_on_one_branch_and_provisions_on_the_other(self, task_environment: str) -> None:
        """`demo-regenerator`: refused on `oeo-refused`, active on `oeo-closed`."""
        output = plain(task_output(run_task("demo-regenerator"), "demo-regenerator"))

        assert self.service_status(task_environment, tasks.REGENERATOR_SERVICE, tasks.OEO_REFUSED_BRANCH) == "rejected"
        assert self.service_status(task_environment, tasks.REGENERATOR_SERVICE, tasks.OEO_CLOSED_BRANCH) == "active"
        assert "provisionable::ProvisionableCheck: FAILED" in output
        assert "provisionable::ProvisionableCheck: PASSED" in output

    # ----------------------------------------------------------------- #
    # The whole walkthrough, then the cleanup
    # ----------------------------------------------------------------- #

    def test_demo_runs_the_ten_walkthrough_steps_in_order(self, task_environment: str) -> None:
        """`demo`: the ten steps run in `WALKTHROUGH` order and every service is decided.

        The long pole of this module. Decided rather
        than active: `demo-refusal` refuses one of the five on purpose, and a
        refusal is an answer.
        """
        output = plain(task_output(run_task("demo"), "demo"))

        positions = [
            output.index(f"{index}/{len(tasks.WALKTHROUGH)} {name}")
            for index, name in enumerate(tasks.WALKTHROUGH, start=1)
        ]
        assert positions == sorted(positions), "the walkthrough steps did not run in order"

        for name in tasks.DEMO_SERVICES:
            status = self.service_status(task_environment, name, tasks.DEMO_BRANCH)
            assert status in {"active", "rejected"}, f"{name} is still {status}, so no generator decided it"

    def test_demo_clean_with_a_branch_removes_only_that_one(self, task_environment: str) -> None:
        """`demo-clean --branch demo`: `demo` goes and the scenario branches stay."""
        task_output(run_task("demo-clean", f"--branch {tasks.DEMO_BRANCH}"), "demo-clean --branch")

        branches = self.branches(task_environment)
        assert tasks.DEMO_BRANCH not in branches
        assert tasks.ODU_BRANCH in branches

    def test_demo_clean_removes_every_scenario_branch(self, task_environment: str) -> None:
        """`demo-clean`: no branch named in `SCENARIO_BRANCHES` is left."""
        task_output(run_task("demo-clean"), "demo-clean")

        branches = self.branches(task_environment)
        left = sorted({row.branch for row in tasks.SCENARIO_BRANCHES} & set(branches))
        assert not left, f"scenario branches the bare form did not remove: {left}"
