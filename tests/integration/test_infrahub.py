"""Load this repository into a live Infrahub and read it back.

A demo whose scenarios are never executed by CI is a demo that is already broken
and nobody has noticed. This is the job that stops that being true.

This layer and `tests/unit/test_geant_dataset.py` catch different failures. The
unit layer catches a claim regression: a seed edit that breaks a route length.
This one catches a load regression: a renamed attribute, an unresolvable
reference, a bound violation, a relationship declared on both sides. A load
defect is invisible to the first layer, so neither layer replaces the other.

Every read assertion checks the GraphQL `errors` array rather than the HTTP
status. A bound violation returns **200** with an `errors` array, so a test that
checks `response.status_code` passes on exactly the failure it was written to
catch. `test_a_bound_violation_returns_200_with_an_errors_array` asserts that
transport behaviour directly, so if a future Infrahub starts returning 4xx this
module says so instead of silently relaxing.

**Everything lives in one class.** `infrahub_testcontainers` exposes its
fixtures through `TestInfrahubDocker`, and every one of them is `scope="class"`:
`infrahub_compose`, `infrahub_app`, `infrahub_port`, `tmp_directory`,
`remote_repos_dir`. A second class is a second Infrahub, a second graph
database, message queue, cache and pair of workers. Test methods run in
definition order, and each one below depends on the state the previous left.

**So `pytest -k` cannot be used to re-run one of these.** Deselecting the
earlier methods deselects the ones that create the branch and load the data, and
every survivor then fails with `Branch: geant-integration not found` at HTTP 404,
which reads like a broken test rather than a broken selection. Run the whole
class or nothing.

**Stop the demo stack before running this.** The two stacks share no port and no
database, but they do share the machine's memory, and during feature 016 the
pair exhausted the container runtime and the test database was killed, failing
twelve of thirteen tests for a reason unrelated to the code.
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
from infrahub_sdk.exceptions import GraphQLError
from infrahub_sdk.protocols import CoreGenericRepository
from infrahub_sdk.testing.docker import TestInfrahubDockerClient
from infrahub_sdk.testing.repository import GitRepo
from infrahub_testcontainers.container import PROJECT_ENV_VARIABLES

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
"""Every kind inheriting OtnOpticalElement. `OtnRouter` is deliberately absent.

Two assertions read this tuple and both are exact: one compares it against the
set of kinds the live generic returns, and one compares the live
`OtnOpticalElement` count against the manifest sum over these kinds. A kind
added to the schema and forgotten here fails both, which is the point.

It has fired once. Feature 017 added `OtnOduSwitch`, which inherits the generic
because `OtnPathHop.element` peers it, and every offline gate stayed green: the
schema loads, the queries pass, and no unit test enumerates the live generic.
Only a query against a loaded graph could tell."""

TARGET_GROUP = "optical_services"
TARGET_GROUP_KIND = "CoreGeneratorGroup"
"""The generator target group `.infrahub.yml` names, and the kind it must be.

The kind matters. The dispatcher does not recognise a `CoreStandardGroup` for
this purpose, and a group of the wrong kind fails silently by never firing,
which is the worse of the two ways to be wrong.

Nothing in this module creates it. `objects/00_groups.yml` does, and the
repository sync is what applies that file. A test that created the group would
pass whether or not the repository can.
"""

EXPECTED_CHECKS = ("channel_collision", "osnr_margin")
"""The two data checks a proposed change must run. `units_import` is the third
registered check; it is not asserted here because it passes or fails on the
worker image rather than on the change."""

pytestmark = pytest.mark.integration


class TestInfrahub(TestInfrahubDockerClient):
    """One Infrahub, loaded once, read by every method below in order."""

    # ----------------------------------------------------------------- #
    # Fixtures
    # ----------------------------------------------------------------- #

    @pytest.fixture(scope="class")
    def address(self, infrahub_port: int) -> str:
        return f"http://localhost:{infrahub_port}"

    @pytest.fixture(scope="class")
    def clean_source(self, tmp_directory: Path) -> Path:
        """A copy of the committed tree, and nothing else.

        `GitRepo.init()` runs `shutil.copytree(..., ignore=ignore_patterns(".git"))`
        and nothing more. Its `directories_to_ignore` field is declared and never
        read. Handing it the working tree copies `.venv/`, `docs/node_modules/`,
        `docs/build/` and both caches, then feeds every file to a pure-Python
        git implementation. That does not raise; it looks like a hang.

        `git archive HEAD` gives exactly the tracked files, which is also the
        more correct input: what Infrahub imports should be what is committed.
        """
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
        """Create the branch and load every object file through `infrahubctl`.

        Through the command-line tool rather than the SDK on purpose: that is
        the loader a demo operator runs, and the failures this layer exists to
        catch are the loader's failures.

        `execute_command` sets `INFRAHUB_ADDRESS` and `INFRAHUB_API_TOKEN` for
        the subprocess. Both are needed: `infrahubctl` resolves the address from
        `.env` but never the token, so an unauthenticated read succeeds while
        the first write fails with "Authentication failure".
        """
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
        """The object files carry numeric load-order prefixes. Renaming one must
        not drop it, and a dropped file loads silently as zero objects.

        `OtnClientSignal` and `OtnCwdmChannel` are both absent from
        `scripts/geant_manifest.json`, which holds the generated kinds only, so
        neither the manifest loop above nor `test_object_counts_match_the_manifest`
        covers them. These two literals are the only assertions in the repository
        that move when a catalog row is added.
        """
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
        """The inheritance boundary, asserted against a live graph.

        Light terminates at a router, so a router contributes no insertion loss,
        so a query against the generic must not return one. Tidying the list by
        adding the generic to the router would break the query the budget engine
        depends on.
        """
        data = self.query(address, "{ OtnOpticalElement { count edges { node { __typename } } } }")
        kinds = {edge["node"]["__typename"] for edge in data["OtnOpticalElement"]["edges"]}
        assert "OtnRouter" not in kinds
        assert kinds == set(OPTICAL_ELEMENT_KINDS)
        assert data["OtnOpticalElement"]["count"] == sum(MANIFEST[kind] for kind in OPTICAL_ELEMENT_KINDS)

    def test_the_congested_corridor_carries_forty_wavelengths(self, address: str) -> None:
        """The capacity headline, counted against loaded objects.

        Filtering carriers by section name is what makes the one-sided
        carrier-to-section relationship enough: no inverse on the section kind
        is needed to answer "how full is this section".
        """
        data = self.query(
            address,
            """{
              direct: OtnOpticalCarrier(sections__name__value: "oms-fra-mil") { count }
              amsfra: OtnOpticalCarrier(sections__name__value: "oms-ams-fra") { count }
              geneva: OtnOpticalCarrier(sections__name__value: "oms-fra-gva") { count }
              quiet:  OtnOpticalCarrier(sections__name__value: "oms-ams-bru") { count }
            }""",
        )
        assert data["direct"]["count"] == 40, "4,134,400 MHz of a 4,800,000 MHz C-band"
        assert data["amsfra"]["count"] == 7, "occupancy is uneven on purpose"
        assert data["geneva"]["count"] == 0, "the alternative route is empty, which is the point"
        assert data["quiet"]["count"] == 0

    def test_inline_amplifiers_have_no_site_and_endpoint_amplifiers_do(self, address: str) -> None:
        """An amplifier hut is not a PoP, and the optional site
        relationship is what lets the model say so instead of inventing one.

        The names carry nothing. Which chain an amplifier is in is which of its
        two section relationships is set, and that is what is asserted here: the
        forward booster has `oms_a2b` and no `oms_b2a`, and its opposite has the
        other. A query naming only one direction would pass against a model that
        had lost the other, so both are read.

        Frankfurt to Milan is nine spans, so twenty amplifiers across ten huts.
        Hut 0 is Frankfurt and takes ordinals 01 and 02; hut 9 is Milan and takes
        19 and 20. The forward chain takes the odd ordinal of each pair, so 01 is
        the booster at Frankfurt, 03 is the first inline hut, and 20 is the
        reverse chain's booster at Milan.
        """
        data = self.query(
            address,
            """{
              inline: OtnAmplifier(name__value: "amp-fra-mil-03") {
                edges { node {
                  site { node { shortname { value } } }
                  oms_a2b { node { name { value } } }
                  oms_b2a { node { name { value } } }
                } }
              }
              booster: OtnAmplifier(name__value: "amp-fra-mil-01") {
                edges { node {
                  site { node { shortname { value } } }
                  oms_sequence { value }
                  oms_a2b { node { name { value } } }
                  oms_b2a { node { name { value } } }
                } }
              }
              reverse: OtnAmplifier(name__value: "amp-fra-mil-20") {
                edges { node {
                  site { node { shortname { value } } }
                  oms_sequence { value }
                  oms_a2b { node { name { value } } }
                  oms_b2a { node { name { value } } }
                } }
              }
            }""",
        )
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
        data = self.query(
            address,
            """{
              OtnSite(shortname__value: "fra") {
                edges { node { tags { edges { node { name { value } } } } } }
              }
              OtnRouter(role__value: "edge") { count }
            }""",
        )
        tags = [edge["node"]["name"]["value"] for edge in data["OtnSite"]["edges"][0]["node"]["tags"]["edges"]]
        assert tags == ["eurohpc-jupiter"]
        assert data["OtnRouter"]["count"] == 6

    def test_the_carrier_resolves_its_channel_and_its_mode(self, address: str) -> None:
        """The quoted-string channel reference resolves to a real channel,
        and the channel renders the same frequency an optical port would."""
        data = self.query(
            address,
            """{
              OtnOpticalCarrier(name__value: "oc-ch047-fra-mil") {
                edges { node {
                  channel { node { display_label center_frequency_display { value } } }
                  optical_mode { node { name { value } } }
                  sections { count }
                } }
              }
            }""",
        )
        node = data["OtnOpticalCarrier"]["edges"][0]["node"]
        assert node["channel"]["node"]["display_label"] == "Ch47"
        assert node["channel"]["node"]["center_frequency_display"]["value"] == "193.65 THz"
        assert node["optical_mode"]["node"]["name"]["value"] == "DP-16QAM 64GBd 400G"
        assert node["sections"]["count"] == 1

    def test_the_error_protocol_every_check_and_transform_here_is_built_on(self, address: str) -> None:
        """A stated exception to the rule that a test must fail on a change to
        this repository and not on a change to Infrahub alone.

        This one fails only if Infrahub changes, and it stays. Every check,
        generator and transform in this repository reads its errors out of a
        200 response body rather than off the status line, so this is the
        executable statement of a premise the demo's own Python is built on.
        Keep it. If it ever fails, the repository's error handling is what
        needs rewriting, not this test.

        It is also the one test that must not use the `query` helper: it checks
        the transport the helper is built on.
        """
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
        """Register this repository in Infrahub and wait for the import.

        Until this ran, nothing in this project had ever exercised the
        proposed-change pipeline: the checks were only ever invoked as terminal
        output and the artifact definition had never produced a file.
        """
        repo = GitRepo(
            name="infrahub-demo-otn",
            src_directory=clean_source,
            dst_directory=remote_repos_dir,
        )
        await repo.add_to_infrahub(client=client)
        in_sync = await repo.wait_for_sync_to_complete(client=client, interval=10, retries=30)

        registered = await client.all(kind=CoreGenericRepository)
        assert registered, "no repository object exists after the create mutation"

        if not in_sync:
            status = registered[0].sync_status.value
            pytest.fail(
                f"Repository import did not reach in-sync (status {status!r}). "
                "Everything that depends on the pipeline is blocked by this."
            )

    def test_the_generator_definition_resolves_its_target_group(self, address: str, default_branch: str) -> None:
        """A generator definition whose target group does not resolve.

        Both `generator_definitions` and `artifact_definitions` name
        `targets: optical_services`, and the only document creating that group
        is `objects/00_groups.yml`. If the repository config stops loading it, a
        clean registration produces a definition with no target: the dispatcher
        never fires, the artifact never renders, and the pipeline goes green
        with no output.

        Asserted on the definition rather than on the group. A group that
        exists proves the object file loaded; a definition whose `targets`
        resolves is the thing that was broken. The peer's kind is asserted too,
        because a `CoreStandardGroup` of the right name is exactly the silent
        failure this guards.

        Read on the default branch: the repository sync applies `objects:`
        there, not onto the branch the dataset test created.
        """
        data = self.query(
            address,
            """{
              CoreGeneratorDefinition(name__value: "optical_service") {
                count
                edges { node { targets { node { __typename display_label } } } }
              }
            }""",
            branch=default_branch,
        )
        definitions = data["CoreGeneratorDefinition"]
        assert definitions["count"] == 1, "the repository sync created no generator definition"

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
        """Open a proposed change and assert the two data checks ran on it.

        **The branch is created here, after the repository is registered.**
        Infrahub creates a matching git branch in every registered repository at
        the moment an Infrahub branch is created. `geant-integration` predates
        the repository, so it has no branch on the repository side and the
        pipeline has no commit to run that repository's checks from, and opening
        the change from it produces `Data Integrity` and `Schema Integrity` and
        nothing else.

        The branch is also deliberately small. Loading the dataset onto it would
        make the diff 1733 objects, and running the whole pipeline over that
        while the graph database is still settling kills Neo4j. One site is
        enough: both checks are global rather than targeted, so they run on any
        change.

        Asserted on validator names, never on a non-empty list. The pipeline is
        asynchronous, so a truthiness check reads an empty list and passes
        vacuously, which is how a pipeline test comes to guard nothing.
        """
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
        # Twelve minutes. With this host to itself the pipeline finishes well
        # inside five, and the rest is headroom for a loaded host: with a second
        # Infrahub stack up on the same Docker daemon no repository validator
        # appears within three hundred seconds, and a window that ends before
        # the pipeline does reports a busy machine as a check that never ran.
        while elapsed < 720:
            try:
                validators = await client.filters(kind="CoreValidator", proposed_change__ids=[change.id])
            except GraphQLError as error:
                # The pipeline pushes both Infrahub and the graph database hard
                # while this polls, and the server answers 503 "Unable to
                # connect to the database" under that load. That is a retry, not
                # a result: raising here reports a database hiccup as a missing
                # check.
                transient = str(error)
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
                f"no validator for {name!r} on the proposed change after {elapsed:.0f}s. "
                f"Validators found: {sorted(found) or 'none'}"
            )
