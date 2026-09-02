"""Load this repository into a live Infrahub and read it back.

This layer and `tests/unit/test_geant_dataset.py` catch different failures. The
unit layer catches a claim regression, a seed edit that breaks a route length.
This one catches a load regression: a renamed attribute, an unresolvable
reference, a bound violation. Neither layer replaces the other.

Read assertions check the GraphQL `errors` array, not the HTTP status: a bound
violation returns 200 with an `errors` array, so a status check passes on
exactly the failure it was written to catch.

**Everything lives in one class, and `pytest -k` cannot re-run one test.** Every
`infrahub_testcontainers` fixture is `scope="class"`, methods run in definition
order, and each depends on the state the previous left. Deselecting the earlier
ones deselects the branch creation and the data load, and every survivor then
fails with `Branch: geant-integration not found` at HTTP 404, which reads like a
broken test rather than a broken selection. Run the whole class or nothing.

**Stop the demo stack before running this.** The two stacks share no port and no
database, but they do share the machine's memory. During feature 016 the pair
exhausted the container runtime and killed the test database, failing twelve of
thirteen tests for a reason unrelated to the code.
"""

from __future__ import annotations

import asyncio
import json
import subprocess  # noqa: S404
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from infrahub_sdk import InfrahubClient
from infrahub_sdk.exceptions import Error as InfrahubSDKError
from infrahub_sdk.protocols import CoreGenericRepository
from infrahub_sdk.testing.docker import TestInfrahubDockerClient
from infrahub_sdk.testing.repository import GitRepo
from infrahub_testcontainers.container import PROJECT_ENV_VARIABLES, InfrahubDockerCompose

from .stack import relax_healthcheck_budgets

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST: dict[str, int] = json.loads((REPO_ROOT / "scripts" / "geant_manifest.json").read_text())
BRANCH = "geant-integration"
PIPELINE_BRANCH = "pipeline-probe"
"""Created after the repository is registered, so the repository has a matching
git branch to run its checks from."""
TOKEN = PROJECT_ENV_VARIABLES["INFRAHUB_TESTING_INITIAL_ADMIN_TOKEN"]

OPTICAL_ELEMENT_KINDS = (
    "OtnFiberSpan",
    "OtnAmplifier",
    "OtnTransponder",
    "OtnRoadm",
    "OtnMuxDemux",
    "OtnPatchPanel",
    "OtnRamanPump",
    "OtnOduSwitch",
)
"""Every kind inheriting OtnOpticalElement. `OtnRouter` is deliberately absent."""

TARGET_GROUP = "optical_services"
TARGET_GROUP_KIND = "CoreGeneratorGroup"
"""The generator target group `.infrahub.yml` names, and the kind it must be."""

EXPECTED_CHECKS = ("channel_collision", "osnr_margin", "carrier_termination")
"""The three data checks a proposed change must run. `units_import` is another."""

POLL_INTERVAL = 10
"""Seconds between polls for anything the task worker does asynchronously."""

REPO_SYNC_TIMEOUT = 900
"""Clone plus import of every check, generator, transform and artifact."""

PIPELINE_TIMEOUT = 1800
"""How long a proposed change may take to produce the checks asserted below, in."""

DEFINITION_TIMEOUT = 600
"""How long a definition may take to appear after the repository reports itself."""

pytestmark = pytest.mark.integration


class TestInfrahub(TestInfrahubDockerClient):
    """One Infrahub, loaded once, read by every method below in order."""

    # ----------------------------------------------------------------- #
    # Fixtures
    # ----------------------------------------------------------------- #

    # `@classmethod` on every class-scoped fixture below. pytest 10 removes the
    # instance-method form: the fixture runs once per class while each test gets
    # a fresh instance, so anything set on `self` would be invisible to the
    # tests. None of these set attributes, so this is the same behaviour without
    # the deprecation.
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
        """The stack `TestInfrahubDocker` builds, with room to start slowly."""
        compose = InfrahubDockerCompose.init(
            directory=tmp_directory,
            version=infrahub_version,
            deployment_type=deployment_type,
        )
        relax_healthcheck_budgets(tmp_directory / "docker-compose.yml")
        return compose

    @pytest.fixture(scope="class")
    @classmethod
    def address(cls, infrahub_port: int) -> str:
        return f"http://localhost:{infrahub_port}"

    @pytest.fixture(scope="class")
    @classmethod
    def clean_source(cls, tmp_directory: Path) -> Path:
        """A copy of the committed tree, and nothing else."""
        export = tmp_directory / "clean-source"
        export.mkdir(parents=True, exist_ok=True)
        archive = subprocess.run(  # noqa: S603
            ["git", "archive", "--format=tar", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        )
        subprocess.run(  # noqa: S603
            ["tar", "-x", "-C", str(export)],  # noqa: S607
            input=archive.stdout,
            check=True,
        )
        return export

    @staticmethod
    def query(address: str, document: str, branch: str = BRANCH) -> dict[str, Any]:
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

    # ----------------------------------------------------------------- #
    # Load
    # ----------------------------------------------------------------- #

    @pytest.mark.asyncio
    async def test_load_schema(self, default_branch: str, client: InfrahubClient, schemas: list[dict]) -> None:
        """Load `schemas/` through the SDK and wait for the schema to converge."""
        await client.schema.wait_until_converged(branch=default_branch)
        response = await client.schema.load(schemas=schemas, branch=default_branch, wait_until_converged=True)
        assert response.errors == {}

    def test_load_objects(self, address: str) -> None:
        """Create the branch and load every object file through `infrahubctl`."""
        for command in (
            f"infrahubctl branch create {BRANCH}",
            f"infrahubctl schema load schemas/ --branch {BRANCH}",
            f"infrahubctl object load objects/ --branch {BRANCH}",
        ):
            result = self.execute_command(command=command, address=address)
            assert result.returncode == 0, f"`{command}` failed:\n{result.stdout}\n{result.stderr}"

    # ----------------------------------------------------------------- #
    # The dataset, read back
    # ----------------------------------------------------------------- #

    def test_every_kind_loaded_the_number_the_manifest_declares(self, address: str) -> None:
        """The manifest is the single source; nothing here hard-codes a count."""
        document = "{" + " ".join(f"{kind}: {kind} {{ count }}" for kind in sorted(MANIFEST)) + "}"
        data = self.query(address, document)
        assert {kind: data[kind]["count"] for kind in MANIFEST} == MANIFEST

    def test_the_five_catalog_files_all_loaded(self, address: str) -> None:
        """The object files carry numeric load-order prefixes. Renaming one must."""
        data = self.query(
            address,
            "{ OtnFrequencyGrid { count } OtnCwdmChannel { count } OtnFiberType { count } "
            "OtnOpticalMode { count } OtnClientSignal { count } }",
        )
        assert data["OtnFrequencyGrid"]["count"] == 96
        assert data["OtnCwdmChannel"]["count"] == 18
        assert data["OtnFiberType"]["count"] == 3
        assert data["OtnOpticalMode"]["count"] == 10
        assert data["OtnClientSignal"]["count"] == 11

    def test_the_optical_element_query_excludes_routers(self, address: str) -> None:
        """The inheritance boundary, asserted against a live graph."""
        data = self.query(address, "{ OtnOpticalElement { count edges { node { __typename } } } }")
        kinds = {edge["node"]["__typename"] for edge in data["OtnOpticalElement"]["edges"]}
        assert "OtnRouter" not in kinds
        assert kinds == set(OPTICAL_ELEMENT_KINDS)
        assert data["OtnOpticalElement"]["count"] == sum(MANIFEST[kind] for kind in OPTICAL_ELEMENT_KINDS)

    def test_the_congested_corridor_carries_forty_wavelengths(self, address: str) -> None:
        """The capacity headline, counted against loaded objects."""
        data = self.query(address, """{.""")
        assert data["direct"]["count"] == 40, "4,134,400 MHz of a 4,800,000 MHz C-band"
        assert data["amsfra"]["count"] == 7, "occupancy is uneven on purpose"
        assert data["geneva"]["count"] == 0, "the alternative route is empty, which is the point"
        assert data["quiet"]["count"] == 0

    def test_inline_amplifiers_have_no_site_and_endpoint_amplifiers_do(self, address: str) -> None:
        """An amplifier hut is not a PoP, and the optional site."""
        data = self.query(address, """{.""")
        inline = data["inline"]["edges"][0]["node"]
        booster = data["booster"]["edges"][0]["node"]
        reverse = data["reverse"]["edges"][0]["node"]

        assert inline["site"]["node"] is None
        assert inline["oms_a2b"]["node"]["name"]["value"] == "oms-fra-mil"
        assert inline["oms_b2a"]["node"] is None

        assert booster["site"]["node"]["shortname"]["value"] == "fra"
        assert booster["oms_sequence"]["value"] == 1
        assert booster["oms_a2b"]["node"]["name"]["value"] == "oms-fra-mil"
        assert booster["oms_b2a"]["node"] is None

        assert reverse["site"]["node"]["shortname"]["value"] == "mil"
        assert reverse["oms_sequence"]["value"] == 1
        assert reverse["oms_a2b"]["node"] is None
        assert reverse["oms_b2a"]["node"]["name"]["value"] == "oms-fra-mil"

    def test_the_eurohpc_tags_reach_their_sites(self, address: str) -> None:
        """A facility is a core tag plus an edge router, not a new kind."""
        data = self.query(address, """{.""")
        tags = [edge["node"]["name"]["value"] for edge in data["OtnSite"]["edges"][0]["node"]["tags"]["edges"]]
        assert tags == ["eurohpc-jupiter"]
        assert data["OtnRouter"]["count"] == 6

    def test_the_carrier_resolves_its_channel_and_its_mode(self, address: str) -> None:
        """The quoted-string channel reference resolves to a real channel,
        and the channel renders the same frequency an optical port would."""
        data = self.query(address, """{.""")
        node = data["OtnOpticalCarrier"]["edges"][0]["node"]
        assert node["channel"]["node"]["display_label"] == "Ch47"
        assert node["channel"]["node"]["center_frequency_display"]["value"] == "193.65 THz"
        assert node["optical_mode"]["node"]["name"]["value"] == "DP-16QAM 64GBd 400G"
        assert node["sections"]["count"] == 1

    def test_the_error_protocol_every_check_and_transform_here_is_built_on(self, address: str) -> None:
        """A stated exception to the rule that a test must fail on a change to."""
        response = httpx.post(
            f"{address}/graphql/{BRANCH}",
            json={
                "query": """mutation {
                  OtnFrequencyGridCreate(data: {
                    channel_number: {value: 97}, center_frequency_mhz: {value: 196150000}
                  }) { ok }
                }"""
            },
            headers={"X-INFRAHUB-KEY": TOKEN},
            timeout=60.0,
        )
        assert response.status_code == 200, "the finding is that a rejection is not a 4xx"
        payload = response.json()
        assert "errors" in payload, "the rejection lives in the body, not the status line"
        assert "97 is higher than the maximum allowed value 96" in json.dumps(payload["errors"])

    # ----------------------------------------------------------------- #
    # The repository, and the pipeline it unlocks
    # ----------------------------------------------------------------- #

    @pytest.mark.asyncio
    async def test_load_repository(
        self,
        client: InfrahubClient,
        remote_repos_dir: Path,
        clean_source: Path,
    ) -> None:
        """Register this repository in Infrahub and wait for the import."""
        repo = GitRepo(
            name="infrahub-demo-otn",
            src_directory=clean_source,
            dst_directory=remote_repos_dir,
        )
        await repo.add_to_infrahub(client=client)
        in_sync = await repo.wait_for_sync_to_complete(
            client=client, interval=POLL_INTERVAL, retries=REPO_SYNC_TIMEOUT // POLL_INTERVAL
        )

        registered = await client.all(kind=CoreGenericRepository)
        assert registered, "no repository object exists after the create mutation"

        if not in_sync:
            status = registered[0].sync_status.value
            pytest.fail(
                f"Repository import did not reach in-sync (status {status!r}). "
                "Everything that depends on the pipeline is blocked by this."
            )

    def test_the_generator_definition_resolves_its_target_group(self, address: str, default_branch: str) -> None:
        """A generator definition whose target group does not resolve."""
        document = """{
          CoreGeneratorDefinition(name__value: "optical_service") {
            count
            edges { node { targets { node { __typename display_label } } } }
          }
        }"""
        started = time.monotonic()
        transient = ""
        definitions: dict[str, Any] = {"count": 0, "edges": []}
        while True:
            try:
                definitions = self.query(address, document, branch=default_branch)["CoreGeneratorDefinition"]
            except (AssertionError, httpx.HTTPError, ValueError) as error:
                # The import this is waiting on is also what loads the server,
                # and under that load it answers 503, sometimes with a body that
                # is not JSON. `query` is written to fail on both, which is right
                # for a single read and wrong inside a poll: here they mean not
                # yet. Kept and reported below, so a genuine outage does not
                # arrive as a bare "no definition". The three cover what `query`
                # can raise: its own assert, the transport, and a body that does
                # not parse.
                transient = f"{type(error).__name__}: {error}"
            if definitions["count"] or time.monotonic() - started >= DEFINITION_TIMEOUT:
                break
            time.sleep(POLL_INTERVAL)

        assert definitions["count"] == 1, (
            "the repository sync created no generator definition within "
            f"{DEFINITION_TIMEOUT}s of the repository reporting itself in sync"
            + (f". Last error while polling: {transient}" if transient else "")
        )

        target = definitions["edges"][0]["node"]["targets"]["node"]
        assert target is not None, (
            f"the generator definition resolves no target group. Nothing under objects/ created {TARGET_GROUP!r}."
        )
        assert target["__typename"] == TARGET_GROUP_KIND, (
            f"the target group is a {target['__typename']}, not a {TARGET_GROUP_KIND}. "
            "The dispatcher does not recognise the other kinds and fails by never firing."
        )
        assert TARGET_GROUP in target["display_label"]

    @pytest.mark.asyncio
    async def test_the_proposed_change_pipeline_runs_the_checks(
        self,
        client: InfrahubClient,
        default_branch: str,
        address: str,
    ) -> None:
        """Open a proposed change and assert the two data checks ran on it."""
        result = self.execute_command(command=f"infrahubctl branch create {PIPELINE_BRANCH}", address=address)
        assert result.returncode == 0, f"branch create failed:\n{result.stdout}\n{result.stderr}"

        site = await client.create(
            kind="OtnSite",
            branch=PIPELINE_BRANCH,
            name="Pipeline probe",
            shortname="ppb",
            description="One object, so the change has a diff and the pipeline has work.",
        )
        await site.save()

        change = await client.create(
            kind="CoreProposedChange",
            name=f"integration-{PIPELINE_BRANCH}",
            source_branch=PIPELINE_BRANCH,
            destination_branch=default_branch,
        )
        await change.save()

        found: set[str] = set()
        transient = ""
        started = time.monotonic()
        elapsed = 0.0
        # See PIPELINE_TIMEOUT. With this host to itself the pipeline finishes
        # well inside five minutes; the rest is headroom for a loaded host,
        # where a window that ends before the pipeline does reports a busy
        # machine as a check that never ran.
        while elapsed < PIPELINE_TIMEOUT:
            try:
                validators = await client.filters(kind="CoreValidator", proposed_change__ids=[change.id])
            except InfrahubSDKError as error:
                # The pipeline pushes both Infrahub and the graph database hard
                # while this polls, and the server answers 503 "Unable to
                # connect to the database" under that load. That is a retry, not
                # a result: raising here reports a database hiccup as a missing
                # check.
                #
                # The SDK's base error, not `GraphQLError`. A 503 arrives in two
                # shapes and only one of them is a GraphQL error: when both API
                # servers are momentarily busy the load balancer answers first,
                # with an HTML body that never parses, and the SDK raises
                # `JsonDecodeError`. That escaped the narrower clause and ended
                # the run outright, which is how a busy moment came to be
                # reported as a broken pipeline. Everything the SDK raises here
                # means the same thing, and the assertion below still fails
                # naming the last one if every poll failed.
                transient = f"{type(error).__name__}: {error}"
                await asyncio.sleep(10)
                elapsed = time.monotonic() - started
                continue
            # `label` is an Attribute on CoreValidator, but an untyped node
            # reaches mypy as the union of every member shape, so read it
            # defensively rather than asserting a type the SDK does not promise.
            found = {str(getattr(validator.label, "value", "") or "") for validator in validators}
            if all(any(name in label for label in found) for name in EXPECTED_CHECKS):
                break
            await asyncio.sleep(10)
            elapsed = time.monotonic() - started

        assert found or not transient, f"every poll failed against the server; last error: {transient}"

        for name in EXPECTED_CHECKS:
            assert any(name in label for label in found), (
                f"no validator for {name!r} on the proposed change after {elapsed:.0f}s "
                f"of a {PIPELINE_TIMEOUT}s window. "
                f"Validators found: {sorted(found) or 'none'}"
            )
