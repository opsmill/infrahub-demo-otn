"""Guard `.infrahub.yml` offline.

Nothing else in the repository validates it. A typo in a `file_path`, a class
name that does not match the Python, or a `query:` key under
`check_definitions` all fail at repository-sync time on a running server, which
is the slowest possible place to find out.

The repository config model uses `extra="forbid"`, so parsing it with the SDK's
own loader catches the shape errors. The rest of this module catches the ones
parsing cannot see: a file that is registered and absent, a class that is
registered and not defined, and a query name the Python class does not bind.
"""

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from infrahub_sdk.ctl.repository import get_repository_config

from tests.unit.conftest import REPO_ROOT, SCRIPT_DIR, schema_files

CONFIG = get_repository_config(REPO_ROOT / ".infrahub.yml")


def _artifacts() -> list[tuple[str, str, str]]:
    """Every registered Python artifact as (kind, file path, class name)."""
    collected = [("check", str(entry.file_path), entry.class_name) for entry in CONFIG.check_definitions]
    collected += [("transform", str(entry.file_path), entry.class_name) for entry in CONFIG.python_transforms]
    return collected


def _generators() -> list[tuple[str, str, str]]:
    """Every registered generator as (name, file path, class name).

    Kept separate from `_artifacts()` because generators bind their query in the
    YAML and checks and transforms bind it on the class. Folding them into one
    list would force the binding test to branch, and a test that branches on the
    thing it is testing is the one that stops testing it.
    """
    return [(entry.name, str(entry.file_path), entry.class_name) for entry in CONFIG.generator_definitions]


def _class_body(path: Path, class_name: str) -> ast.ClassDef:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"{path.name} defines no class {class_name}")


@pytest.mark.parametrize("entry", CONFIG.queries, ids=lambda entry: entry.name)
def test_every_registered_query_file_exists(entry: object) -> None:
    path = REPO_ROOT / str(getattr(entry, "file_path"))
    assert path.is_file(), f"{path} is registered and missing"
    assert path.suffix == ".gql", f"{path} is registered as a query and is not a .gql file"


def _bound_query(path: Path, class_name: str) -> str | None:
    """The query name a check or transform class binds, or `None` when it binds none."""
    body = _class_body(path, class_name)
    for node in body.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "query" for target in node.targets)
            and isinstance(node.value, ast.Constant)
        ):
            return str(node.value.value)
    return None


@pytest.mark.parametrize(("kind", "file_path", "class_name"), _artifacts())
def test_every_registered_artifact_defines_the_class_it_names(kind: str, file_path: str, class_name: str) -> None:
    path = REPO_ROOT / file_path
    assert path.is_file(), f"{path} is registered as a {kind} and is missing"
    _class_body(path, class_name)


@pytest.mark.parametrize(("kind", "file_path", "class_name"), _artifacts())
def test_every_artifact_binds_a_query_that_is_registered(kind: str, file_path: str, class_name: str) -> None:
    """The class binds the query, not the YAML.

    `check_definitions` takes no `query:` key, and putting one there fails the
    whole repository config because the model forbids extras.
    `generator_definitions` is the one that does take a top-level query, and the
    two are not interchangeable. So the binding lives on the class, and this is
    what checks it points somewhere real.
    """
    declared = {entry.name for entry in CONFIG.queries}
    bound = _bound_query(REPO_ROOT / file_path, class_name)
    assert bound, f"{class_name} sets no `query` attribute"
    assert bound in declared, f"{class_name} binds query {bound!r}, which .infrahub.yml does not register"


@pytest.mark.parametrize(("name", "file_path", "class_name"), _generators())
def test_every_generator_defines_the_class_it_names(name: str, file_path: str, class_name: str) -> None:
    path = REPO_ROOT / file_path
    assert path.is_file(), f"{path} is registered as generator {name} and is missing"
    _class_body(path, class_name)


@pytest.mark.parametrize(("name", "file_path", "class_name"), _generators())
def test_every_generator_declares_a_registered_query_in_the_yaml(name: str, file_path: str, class_name: str) -> None:
    """The opposite rule to the one above, and the reason this test exists.

    `generator_definitions` requires a top-level `query:`; `check_definitions`
    forbids one, because the repository config model uses `extra="forbid"`. That
    one field is the whole shape difference between the two sections and it is
    the registration error that shows up most often. Iterating checks and
    transforms alone guards the field nothing gets wrong and leaves the field
    everyone gets wrong unguarded, so generator definitions are covered here too.
    """
    entry = next(item for item in CONFIG.generator_definitions if item.name == name)
    declared = {query.name for query in CONFIG.queries}
    assert entry.query, f"generator {name} declares no query, which generator_definitions requires"
    assert entry.query in declared, (
        f"generator {name} names query {entry.query!r}, which .infrahub.yml does not register"
    )
    assert entry.targets, f"generator {name} declares no targets group"


def test_no_check_definition_carries_a_query_key() -> None:
    """The shape difference, asserted from the other side.

    A `query:` under `check_definitions` fails the whole repository config at
    sync time. The SDK's own loader would already have raised while parsing, so
    reaching this assertion means the file parsed; what it pins is that nobody
    "fixed" the loader's complaint by loosening the model.
    """
    raw = yaml.safe_load((REPO_ROOT / ".infrahub.yml").read_text())
    offenders = [entry.get("name") for entry in raw.get("check_definitions") or [] if "query" in entry]
    assert not offenders, f"check_definitions must not carry a `query:` key: {offenders}"


def test_the_repository_config_does_not_load_the_demo_directory() -> None:
    """`demo/` is scenario scaffolding, not data.

    `demo/90_fra_mil_saturated.yml` fills the Frankfurt to Milan corridor at both
    layers: 96 of 96 channels, and one 80-slot tenant on each of the forty
    wavelengths that terminate there, so the 400G refusal has something to refuse
    and the 100G beside it has somewhere to groom. Loading it with the dataset
    would make the shipped capacity claim of 71 occupied and 25 free false, and
    `test_geant_dataset.py` asserts that claim. The demo guide loads it explicitly
    for the one scenario that needs it.
    """
    raw = yaml.safe_load((REPO_ROOT / ".infrahub.yml").read_text())
    referenced = [str(value) for value in raw.get("objects") or []]
    assert not any("demo" in path for path in referenced), (
        f"the repository config must not load demo scaffolding: {referenced}"
    )


CONTENT_TYPES = {
    "text/plain",
    "text/csv",
    "text/markdown",
    "application/json",
    "application/yaml",
    "application/xml",
    "application/hcl",
    "image/svg+xml",
}
"""The eight values the server validates `content_type` against.

Anything outside this set fails at repository-sync time with a message about an
enum, and it fails the whole file rather than the one entry that is wrong.
"""

ARTIFACT_TARGET_KINDS = {"OtnService", "OtnSite"}
"""The kinds that inherit `CoreArtifactTarget` and can therefore hold artifacts.

`OtnOpticalCarrier` is deliberately not here. Promoting the budget report to a
stored artifact would mean adding the inheritance to the kind holding 71 loaded
objects, which is a schema migration.
"""


@pytest.mark.parametrize("entry", CONFIG.artifact_definitions, ids=lambda entry: getattr(entry, "name", "none"))
def test_every_artifact_definition_resolves(entry: object) -> None:
    """The four strings an artifact definition binds by name and not by reference.

    `transformation:` resolves by exact match against `python_transforms` or
    `jinja2_transforms`, with no namespace and no kind. A typo or a stale rename
    surfaces as "transformation not found" at render time, long after the rest of
    the repository has synced cleanly, which is the slowest possible place to
    learn about a five-character mistake.
    """
    transforms = {item.name for item in CONFIG.python_transforms}
    transforms |= {item.name for item in getattr(CONFIG, "jinja2_transforms", [])}
    name = str(getattr(entry, "name"))
    transformation = str(getattr(entry, "transformation"))
    content_type = str(getattr(entry, "content_type"))

    assert transformation in transforms, (
        f"artifact {name} names transformation {transformation!r}, which .infrahub.yml does not register"
    )
    assert content_type in CONTENT_TYPES, (
        f"artifact {name} declares content type {content_type!r}, not in the allowlist"
    )
    assert getattr(entry, "targets"), f"artifact {name} declares no targets group"
    assert getattr(entry, "artifact_name"), f"artifact {name} declares no artifact_name"


def test_no_artifact_definition_targets_a_kind_that_cannot_hold_one() -> None:
    """An artifact target must inherit `CoreArtifactTarget` on the concrete node.

    The config names a *group*, not a kind, so nothing in the YAML says which
    kind will end up in it. This pins the intent from the other side: the
    parameters an artifact binds must be readable on a kind that can hold an
    artifact, and `ARTIFACT_TARGET_KINDS` is the list of those.

    Every schema file is read rather than the one that holds the services.
    `OtnService` and `OtnSite` are declared in different files, and a test that
    reads one file passes for the kind it knows about while saying nothing about
    the other.
    """
    inheriting = {
        f"{node['namespace']}{node['name']}"
        for path in schema_files()
        for node in (yaml.safe_load(path.read_text()) or {}).get("nodes") or []
        if "CoreArtifactTarget" in (node.get("inherit_from") or [])
    }
    assert ARTIFACT_TARGET_KINDS <= inheriting, (
        f"{ARTIFACT_TARGET_KINDS - inheriting} are named as artifact targets and do not inherit CoreArtifactTarget"
    )


def test_the_generator_query_reaches_its_roadms_through_the_endpoint_sites() -> None:
    """The shape the generator depends on, pinned offline.

    The two ROADMs a dispatch needs are reached through
    `endpoint_a -> site -> devices` and `endpoint_z -> site -> devices`. A
    top-level `OtnRoadm` collection would fetch every ROADM in the network on
    every dispatch to build a map with two used keys, and nothing else offline
    would catch it.

    The ceiling is six because feature 017 added `OtnOduSwitch`, which is the one
    added fetch R-008 predicted: the section route still comes from the traversal,
    and covering it with carriers needs the devices and the carriers each one
    terminates. Raising this number is a decision, not a formality.

    Top-level fields are counted by indentation: at two spaces, inside the query
    block, every line ending in `{` opens a collection.
    """
    document = (REPO_ROOT / "queries" / "optical_service.gql").read_text()
    top_level = [
        line.strip().removesuffix(" {")
        for line in document.splitlines()
        if line.startswith("  ") and not line.startswith("   ") and line.rstrip().endswith("{")
    ]
    assert "OtnRoadm" not in top_level, (
        "optical_service.gql fetches every ROADM in the network again. "
        "The two the run needs are one traversal from the service's endpoints."
    )
    assert len(top_level) <= 6, f"optical_service.gql has grown to {len(top_level)} top-level collections: {top_level}"

    for endpoint in ("endpoint_a", "endpoint_z"):
        start = document.index(endpoint)
        window = document[start : start + 600]
        assert "site" in window and "devices" in window, (
            f"{endpoint} no longer reaches its site's devices, so the anchoring ROADM is unreachable"
        )


WATCHABLE_SECTIONS = ("python_transforms", "jinja2_transforms", "generator_definitions")
"""The sections the repository config model accepts a `watch` key on.

`check_definitions` is absent on purpose, and not as an oversight: the model has
no such field there and uses `extra="forbid"`, so a `watch` on a check fails the
whole file at sync rather than the one entry that carries it.
"""


def test_every_definition_that_can_watch_declares_it() -> None:
    """Infrahub never analyses Python imports.

    Until `watch` is declared it assumes a definition has dependencies it cannot
    see, and ties the definition's fingerprint to the repository's current
    commit, so every commit re-fingerprints it. Every transform and the
    generator import `infrahub_demo_otn`, so every one of them needs the
    declaration and the empty-list form would be a false claim.

    `check_definitions` is absent on purpose: the config model does not accept
    `watch` there, so a check cannot make the declaration at all.
    """
    missing = [
        f"{section}.{entry.name}"
        for section in WATCHABLE_SECTIONS
        for entry in getattr(CONFIG, section)
        if not (entry.watch and entry.watch.files)
    ]
    assert not missing, f"no watch.files on {missing}"


def test_every_python_transform_declares_watch_files() -> None:
    """The same rule, asserted over the section this feature adds an entry to.

    The rule above sweeps three sections and reports the first gap it finds
    anywhere. This one names the transforms, so a transform added without the
    declaration reads as a transform problem rather than as a config problem
    somewhere in the file.
    """
    missing = [entry.name for entry in CONFIG.python_transforms if not (entry.watch and entry.watch.files)]
    assert not missing, f"python_transforms entries with no watch.files: {missing}"


def test_every_watched_path_exists() -> None:
    """A watch path that resolves to nothing declares a dependency on nothing.

    Infrahub does not validate the path. A typo makes the definition look
    declared while behaving like the undeclared case it was written to fix, and
    the only symptom is a fingerprint that moves on every commit.
    """
    missing = [
        f"{section}.{entry.name}: {watched}"
        for section in WATCHABLE_SECTIONS
        for entry in getattr(CONFIG, section)
        for watched in (entry.watch.files if entry.watch else [])
        if not (REPO_ROOT / str(watched)).exists()
    ]
    assert not missing, f"watch paths that resolve to nothing: {missing}"


WATCH_GENERATOR = SCRIPT_DIR / "generate_watch_lists.py"


def test_the_watch_lists_match_a_fresh_derivation() -> None:
    """`.infrahub.yml` holds generated watch lists, so they are diffed like any.

    Each list is one definition's transitive import closure over
    `infrahub_demo_otn`, derived from the AST. A hand edit and a new import both
    fail here, which is the whole reason a narrowed list is allowed at all: a
    hand-maintained one is right the day it is written and silently wrong
    afterwards, and the symptom is an artifact that renders cleanly from code
    that is not on the branch.

    The derivation runs its own dynamic-import scan first, so this also fails on
    the day someone adds an `importlib` call under `src/`, `transforms/`,
    `generators/` or `checks/`.
    """
    result = subprocess.run(
        [sys.executable, str(WATCH_GENERATOR), "--check"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_derivation_refuses_to_narrow_around_a_dynamic_import(tmp_path: Path) -> None:
    """The fail-loud path, exercised rather than assumed.

    A static walk cannot see a module reached through `importlib`, `__import__`,
    `exec` or `eval`, and a watch list that misses one is exactly the stale
    artifact the narrowing was supposed to prevent. The scan has to stop the
    derivation, not drop the definition it found, so nothing is narrowed on
    evidence the walk has already admitted is incomplete.
    """
    spec = importlib.util.spec_from_file_location(WATCH_GENERATOR.stem, WATCH_GENERATOR)
    assert spec and spec.loader, f"{WATCH_GENERATOR} could not be loaded"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for source in ("import importlib\n", "from importlib import import_module\n", "x = __import__('os')\n"):
        offender = tmp_path / "offender.py"
        offender.write_text(source)
        assert module.dynamic_import_sites(ast.parse(source, filename=str(offender))), source

    assert not module.dynamic_import_sites(ast.parse("from infrahub_demo_otn.units import km_to_m\n"))


MAP_ARTIFACTS = {"network_map": "network-map", "odu_map": "odu-map"}
"""The two SVG artifacts every point of presence carries, and their file names.

Two, and they are two rather than one because they answer different questions
about the same fifteen sites: one states an OSNR margin per section and the other
states what is inside the wavelengths. Neither is a mode of the other.
"""


@pytest.mark.parametrize(("name", "artifact_name"), sorted(MAP_ARTIFACTS.items()))
def test_the_map_artifact_renders_svg_onto_the_sites_it_names(name: str, artifact_name: str) -> None:
    """The four strings a map artifact binds, and the one that is a variable.

    `content_type` has to be `image/svg+xml`, because both transforms return a
    `str` holding an SVG document. Paired with a text content type it would
    still render; paired with a JSON one the body becomes a quoted string.

    `parameters` maps the query variable to an attribute path on the target.
    The key is `site` because each query declares `$site`, and the value is
    `shortname__value` because that is what the query filters on. Swapping the
    two renders fifteen copies of the same picture and nothing complains.

    `transformation` has to match a registered transform name exactly. A
    mismatch fails at render time with "transformation not found", long after the
    repository has synced cleanly, which is the slowest place to learn it.
    """
    entry = next((item for item in CONFIG.artifact_definitions if item.name == name), None)
    assert entry is not None, f".infrahub.yml registers no {name} artifact definition"
    assert entry.artifact_name == artifact_name, entry.artifact_name
    assert entry.content_type == "image/svg+xml", entry.content_type
    assert entry.content_type in CONTENT_TYPES
    assert entry.transformation == name
    assert entry.transformation in {item.name for item in CONFIG.python_transforms}
    assert entry.targets == "otn_sites"
    assert entry.parameters == {"site": "shortname__value"}

    document = (REPO_ROOT / "queries" / f"{name}.gql").read_text()
    assert "$site: String!" in document, f"{name}.gql no longer declares the variable the artifact binds"


def test_each_map_transform_binds_its_own_query_and_not_the_other_one() -> None:
    """Two maps, two queries, and the pair is easy to cross.

    The ODU query deliberately selects no spans, no fibre types, no amplifiers
    and no mode catalog, because it computes no budget. A transform bound to the
    other query would still render: the fields it reads are in both payloads, and
    what it would cost is every ODU render paying for a margin the ODU map states
    nothing about.
    """
    bound = {
        entry.name: _bound_query(REPO_ROOT / str(entry.file_path), entry.class_name)
        for entry in CONFIG.python_transforms
        if entry.name in MAP_ARTIFACTS
    }
    assert bound == {name: name for name in MAP_ARTIFACTS}


def test_the_pipeline_runs_nine_checks() -> None:
    """Nine, and one of them carries a name that was deleted in `ef89265`.

    The deleted `monitor_completeness` asserted that a monitor carried the
    readings its family reports and none it cannot take. Both halves are schema
    constraints now: the readings are mandatory on each monitor kind, and a kind
    has no field for a reading its hardware cannot produce. A check that can
    never fail is worse than no check, because a green result reads as evidence.

    Feature 024 registered the name again, against a different question: whether
    a device has a monitor at all. The deleted check could not answer that one,
    because it iterated over monitors and a device carrying none was invisible to
    it. So the assertion this test used to make, that no query of that name
    exists, has become false and keeping it would only pin the absence of a file
    that is now supposed to be there.

    What replaces it is the pair the reused name has to satisfy, which is
    stronger than the absence was: a query of that name is registered, and the
    class the definition names binds it. A future edit that revives the old
    question under the old name would still pass here, and the docstring in
    `checks/monitor_completeness.py` is what stands against that, along with the
    tests in `tests/unit/test_checks.py` that pin what the new one reports.

    `provisionable` arrived with feature 022. It is the opposite case to the
    deleted check: the schema carries everything it can, which is the six reason
    codes, the 512-character detail and the boolean default, and what is left is
    a rule about merging rather than about writing.
    """
    names = sorted(entry.name for entry in CONFIG.check_definitions)
    assert names == [
        "carrier_termination",
        "channel_collision",
        "channel_count_consistency",
        "container_capacity",
        "diversity",
        "monitor_completeness",
        "osnr_margin",
        "provisionable",
        "units_import",
    ]
    assert "monitor_completeness" in {entry.name for entry in CONFIG.queries}
    revived = next(entry for entry in CONFIG.check_definitions if entry.name == "monitor_completeness")
    assert revived.class_name == "MonitorCompletenessCheck"
    assert _bound_query(REPO_ROOT / str(revived.file_path), revived.class_name) == "monitor_completeness"


# Every directory a reader would call production code, plus the tests, because
# a test that recovers a fact from a name teaches the convention just as
# effectively as a module that does.
SEARCHED_DIRECTORIES = ("src", "checks", "transforms", "generators", "queries", "scripts", "tests")

NAME_PARSING = re.compile(
    r"""
    ["'](?:-(?:bst|pre|il)\d*)["']
    |\.(?:startswith|endswith|split|partition|rpartition|removeprefix|removesuffix)\(\s*["']-
    |\.(?:startswith|removeprefix)\(\s*[A-Z_]*PREFIX
    |\[\s*len\(\s*[A-Z_]*PREFIX
    """,
    re.VERBOSE,
)
"""What recovering meaning from a device name looks like.

Four shapes, all narrow on purpose. The first is a bare role-suffix literal:
`-bst`, `-pre` and `-il` only ever existed in this repository to be matched
against, so the literal appearing at all is the defect. The second is any
string-slicing call taking a hyphenated literal, which is how a hyphen-separated
name gets taken apart.

Narrow, because the obvious wide rule does not work. "Anything calling
`.endswith` on something called name" flags every `_display` suffix test in the
schema contract module, and that reads a *schema attribute* name, which is not
this defect at all. Naming an amplifier in a query or building one in the
generator is not the defect either: what FR-026 forbids is reading a name back
to recover a direction, a role or a position.

The third and fourth shapes were added by feature 019, after this guard missed
the finding that feature existed to remove. Two transforms recovered a EuroHPC
facility's name from a tag whose prefix was held in a module constant,
written as a `startswith` against that constant followed by a slice at its
length. That reads a name for meaning exactly as much as `-bst` does, and it
slipped past because the prefix lived in a constant rather than in a hyphenated
literal. So a
constant named `*PREFIX`, used with `startswith`, `removeprefix` or a `len()`
slice, is now the same defect.

**Prefixes only, and that is deliberate.** The first attempt covered `*SUFFIX`
too and immediately flagged `test_schema_contract.py`, which calls
`.endswith(UNIT_SUFFIXES)` on a *schema attribute* name. The paragraph above
already warned that the obvious wide rule does not work, and widening it proved
the warning right within a minute.

The `a2b` and `b2a` tokens have their own test below, because they appear in
relationship names too and need a different pattern."""

NAME_PARSING_EXEMPT = {
    "src/infrahub_demo_otn/monitors.py": (
        "A degree port's name is the only thing that says which section it faces. "
        "OtnRoadmDegreePort holds no relationship to OtnOpticalMultiplexSection, in "
        "either direction and at any depth, so there is no edge to read instead. "
        "R-005 in specs/024-monitor-consistency-checks/research.md records the "
        "relationship as the next change to make, and until it exists this file is "
        "where the convention is declared once, in both directions, with "
        "tests/unit/test_monitors.py binding the pair by round trip. That is the "
        "exact mitigation feature 016 lacked when it read an owning service out of "
        "a container's name and reported a neighbour's containers as part of a "
        "service's circuit."
    ),
}
"""Files allowed to read a name, each with the reason and what makes it safe.

An exemption is a debt, so it is written where the rule is rather than as a
comment in the file taking it, and the test below refuses one that has stopped
being used. A file that no longer parses a name must lose its entry, or the next
person to add a `startswith` to it inherits a permission nobody granted them."""


def test_an_exemption_from_the_name_parsing_rule_is_still_being_used() -> None:
    """A stale exemption is worse than no rule, because it reads as reviewed.

    The file must exist and must still contain the parsing the entry excuses. If
    the relationship R-005 asks for arrives and the suffix join goes away, this
    fails until the entry goes with it."""
    for relative in NAME_PARSING_EXEMPT:
        path = REPO_ROOT / relative
        assert path.exists(), f"{relative} is exempt from the name-parsing rule and does not exist"
        assert any(NAME_PARSING.search(line) for line in path.read_text().splitlines()), (
            f"{relative} no longer reads a name, so its exemption is stale and should be deleted"
        )


def test_nothing_reads_a_device_name_for_meaning() -> None:
    """A name is an identifier. Everything it used to say is in the schema.

    The chain an amplifier is in is the relationship holding it, its position is
    `oms_sequence`, and whether it is a booster or a pre-amplifier is the `role`
    on its own IN and OUT ports. A reader who wants the forward chain of a
    section should not have to know a naming convention to ask for one, and the
    name is the one copy of a fact the schema cannot validate.

    Any deterministic naming rule over an ordered set is invertible, so the
    requirement is not that a name hides a fact. It is that nothing reads it.

    `NAME_PARSING_EXEMPT` is the one way past this, and it is a list of one. An
    exemption states which relationship is missing and what stands in for it
    until that relationship exists.
    """
    offenders: list[str] = []
    for directory in SEARCHED_DIRECTORIES:
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if path.suffix not in (".py", ".gql") or "__pycache__" in path.parts:
                continue
            if str(path.relative_to(REPO_ROOT)) in NAME_PARSING_EXEMPT:
                continue
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#") or NAME_PARSING.search(line) is None:
                    continue
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {stripped}")
    assert not offenders, "code reading a device name for meaning:\n" + "\n".join(offenders)


def test_no_source_file_carries_a_direction_token_in_a_device_name() -> None:
    """No `a2b` or `b2a` in a name anywhere, in the dataset or in a fixture.

    `amplifiers_a2b` and `oms_b2a` are relationship names and are the point of
    the change, so the pattern here is the hyphenated form a device name would
    use.
    """
    token = re.compile(r"-(?:a2b|b2a)[-\"']")
    offenders: list[str] = []
    for directory in (*SEARCHED_DIRECTORIES, "objects", "demo", "schemas"):
        for path in sorted((REPO_ROOT / directory).rglob("*")):
            if path.suffix not in (".py", ".gql", ".yml", ".json") or "__pycache__" in path.parts:
                continue
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if token.search(line):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}: {line.strip()}")
    assert not offenders, "device names carrying a direction token:\n" + "\n".join(offenders)


def test_the_invoke_check_list_names_every_registered_check() -> None:
    """`tasks.py::CHECKS` is a hand-kept mirror of `check_definitions`, and it
    drifted twice.

    `container_capacity` arrived with feature 016 and `diversity` with 017, and
    neither reached the tuple, so `invoke check` ran three of the five and
    `invoke check --name diversity` refused a check that exists. Nothing failed:
    a shorter list is a quieter demo, not an error. This is the assertion that
    would have said so.

    `tasks.py` is read rather than imported. Importing it pulls in invoke, rich
    and a docker probe to answer a question about one tuple of strings.
    """
    module = ast.parse((REPO_ROOT / "tasks.py").read_text())
    listed: tuple[str, ...] = ()
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "CHECKS" for target in node.targets
        ):
            listed = tuple(str(element.value) for element in node.value.elts)  # type: ignore[attr-defined]
    assert listed, "tasks.py no longer declares a CHECKS tuple"
    assert set(listed) == {entry.name for entry in CONFIG.check_definitions}
