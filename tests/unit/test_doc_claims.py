"""The figures the documentation publishes, read back out of the pages.

Every other claims module in this suite runs the same direction: it recomputes a
value from `objects/` or `src/` and asserts the engine produces what the pages
are supposed to say. `test_geant_dataset.py` is the clearest case. It asserts
`total == 2190` with the message "the pages say the load is 2190 objects", and
that message is the whole problem: nothing had read a page. A human was trusted
to copy the number across, and when `tasks.py` announced 2243 objects for a load
of 2190 the suite stayed green for three commits.

So this module runs the other direction. It opens the `.mdx`, pulls the figure a
reader will actually see out of the prose, and compares it against the same
ground truth the rest of the suite computes from. A number can still be wrong
here, but it can no longer be wrong in one place and right in another.

**Why regexes over prose, rather than importing the values into MDX.** Docusaurus
can import JSON and interpolate it, which would make drift impossible by
construction. It would also replace "2190 objects" with `{manifest.total}` in a
body of writing that is the demo's product. The duplication is kept and the
disagreement is made unmergeable instead, which costs one test module and no
sentences.

Each test names the pages it reads and the source it trusts. When one fails, the
fix is nearly always the page: the sources here are generated, asserted
elsewhere, or both.
"""

import json
import re
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import tasks
import yaml
from invoke import Collection

from infrahub_demo_otn.budget import SYSTEM_MARGIN_MDB
from infrahub_demo_otn.containers import SLOT_TABLE
from infrahub_demo_otn.odudraw import HEADROOM_BAND_EDGES_SLOTS, HEADROOM_BANDS
from infrahub_demo_otn.routing import REASON_PRECEDENCE
from infrahub_demo_otn.units import mdb_to_db
from tests.unit.conftest import (
    DOC_DIR,
    REPO_ROOT,
    SCHEMA_DIR,
    SCRIPT_DIR,
    demo_objects_of_kind,
    doc_text,
    object_documents,
    objects_of_kind,
)

MANIFEST = json.loads((SCRIPT_DIR / "geant_manifest.json").read_text())

REPOSITORY_CONFIG = yaml.safe_load((REPO_ROOT / ".infrahub.yml").read_text())

BUILT_IN_VALIDATORS = 4
"""The validators a proposed change runs that no entry in `.infrahub.yml` asks for.

One `CoreDataValidator`, one `CoreSchemaValidator` and two
`CoreRepositoryValidator`. The figure comes from a live proposed change opened
while feature 023 was written: six user validators, one generator, three
artifacts and these four made fourteen, and fourteen is what the page said until
two more checks were registered.

Written as a constant rather than derived, because nothing in this repository
declares it. A stack that registers a third repository moves it, and then this
number and the page move together.
"""

OPTICAL_ELEMENT_KINDS = (
    "OtnAmplifier",
    "OtnFiberSpan",
    "OtnMuxDemux",
    "OtnOduSwitch",
    "OtnPatchPanel",
    "OtnRamanPump",
    "OtnRoadm",
    "OtnTransponder",
)
"""The eight kinds that inherit `OtnOpticalElement`.

Pinned identically in `test_schema_contract.py`, which asserts the list against
the schema. Repeated here rather than imported so a failure in that module and a
failure in this one say different things.
"""


def figure(page: str, pattern: str) -> str:
    """The one capture group `pattern` finds in `page`, or a failure naming both.

    Deliberately strict about finding exactly one match. A page that grew a
    second sentence saying the same thing is the condition this module exists to
    catch, so two matches is a failure rather than a silent choice of the first.
    """
    found = re.findall(pattern, doc_text(page))
    assert len(found) == 1, f"{page}: expected one match for {pattern!r}, found {len(found)}: {found}"
    return found[0]


# ---------------------------------------------------------------------------
# What the load is
# ---------------------------------------------------------------------------


def test_the_object_total_every_page_publishes_is_the_total_that_loads() -> None:
    """`installation-setup.mdx` and `provisioning-scenarios.mdx` both quote the
    size of the load, and `tasks.py` prints it while the load runs.

    Three copies of one number, and the copy in `tasks.py` is the one a reader
    sees first. It said 2243 against a real 2190 until the documentation review
    caught it, which is the drift this test exists to make unmergeable.
    """
    actual = sum(len((document.get("spec") or {}).get("data") or []) for document in object_documents())

    install = int(figure("installation-setup.mdx", r"object load takes a few minutes for ([\d,]+) objects"))
    provisioning = int(figure("provisioning-scenarios.mdx", r"already holding the ([\d,]+)-object dataset"))

    banner = re.findall(r"objects, about a minute for ([\d,]+) of them", (REPO_ROOT / "tasks.py").read_text())
    assert len(banner) == 1, "tasks.py should print the object total exactly once"

    assert install == actual, f"installation-setup.mdx says {install} objects, {actual} load"
    assert provisioning == actual, f"provisioning-scenarios.mdx says {provisioning} objects, {actual} load"
    assert int(banner[0]) == actual, f"tasks.py announces {banner[0]} objects, {actual} load"


def test_the_optical_element_count_the_install_page_publishes_is_the_manifest() -> None:
    """`installation-setup.mdx` publishes the count `invoke inventory` reports.

    The count is every kind inheriting `OtnOpticalElement`, and the ODU switches
    inherit it too. The page said 532 and omitted them; the server answers 535.
    """
    expected = sum(MANIFEST[kind] for kind in OPTICAL_ELEMENT_KINDS)
    published = int(figure("installation-setup.mdx", r"It counts (\d+) optical elements"))
    assert published == expected, f"the page says {published}, the manifest gives {expected}"


def test_the_inventory_sentence_is_the_dataset() -> None:
    """`installation-setup.mdx` opens the data section with the inventory a reader
    uses to decide whether the demo is worth twenty minutes.

    Seven figures in two sentences, each of which the seed can move.
    """
    devices = (
        "OtnRouter",
        "OtnTransponder",
        "OtnRoadm",
        "OtnAmplifier",
        "OtnMuxDemux",
        "OtnPatchPanel",
        "OtnRamanPump",
        "OtnOduSwitch",
    )
    ports = (
        "OtnRouterPort",
        "OtnClientPort",
        "OtnLinePort",
        "OtnRoadmAddDropPort",
        "OtnRoadmDegreePort",
        "OtnAmplifierPort",
        "OtnTributaryPort",
        "OtnAmplifierMonitor",
        "OtnRoadmDegreeMonitor",
        "OtnMuxDemuxMonitor",
        "OtnRamanMonitor",
        "OtnReceiverMonitor",
    )
    published = {
        "sites": int(figure("installation-setup.mdx", r"network: (\d+) sites")),
        "sections": int(figure("installation-setup.mdx", r"(\d+) ROADM-to-ROADM sections")),
        "spans": int(figure("installation-setup.mdx", r"(\d+) fiber\nspans")),
        "devices": int(figure("installation-setup.mdx", r"sit (\d+) devices")),
        "ports": int(figure("installation-setup.mdx", r"devices, ([\d,]+) ports")),
        "wavelengths": int(figure("installation-setup.mdx", r"ports, (\d+) pre-provisioned wavelengths")),
    }
    actual = {
        "sites": len(objects_of_kind("OtnSite")),
        "sections": len(objects_of_kind("OtnOpticalMultiplexSection")),
        "spans": len(objects_of_kind("OtnFiberSpan")),
        "devices": sum(len(objects_of_kind(kind)) for kind in devices),
        "ports": sum(len(objects_of_kind(kind)) for kind in ports),
        "wavelengths": len(objects_of_kind("OtnOpticalCarrier")),
    }
    assert published == actual


# ---------------------------------------------------------------------------
# What the schema is
# ---------------------------------------------------------------------------


def test_the_device_kind_count_is_the_same_wherever_the_reference_says_it() -> None:
    """`schema-reference.mdx` states the number of device kinds three times: in the
    files table, in the two-generics section and in the sidebar section.

    The sidebar sentence said seven while the other two said eight, which is the
    kind of self-contradiction a page this long produces on its own.
    """
    parsed = yaml.safe_load((SCHEMA_DIR / "otn_devices.yml").read_text())
    actual = sum(1 for node in parsed.get("nodes", []) if "OtnGenericDevice" in (node.get("inherit_from") or []))

    page = doc_text("schema-reference.mdx")
    spelled = {"seven": 7, "eight": 8, "nine": 9}
    said = {spelled[word] for word in re.findall(r"(seven|eight|nine) device kinds", page)}

    assert said == {actual}, f"the page says {sorted(said)} device kinds, the schema defines {actual}"


def test_the_rejection_code_count_is_the_python_and_the_schema() -> None:
    """`concepts.mdx`, `provisioning-scenarios.mdx` and `schema-reference.mdx` all
    say how many rejection codes there are, and the reference also says where they
    live. It claimed five in `routing.py` and one in `optical_service.py`; all six
    are in `routing.py`, and the source comment there says why.
    """
    routing = (REPO_ROOT / "src" / "infrahub_demo_otn" / "routing.py").read_text()
    defined = set(re.findall(r"^REASON_([A-Z_]+) = \"", routing, re.MULTILINE))
    assert len(defined) == len(REASON_PRECEDENCE) + 1, "REASON_NO_SLOTS is the one outside the precedence order"

    generator = (REPO_ROOT / "generators" / "optical_service.py").read_text()
    assert not re.findall(r"^REASON_[A-Z_]+ = \"", generator, re.MULTILINE), (
        "schema-reference.mdx says every rejection code lives in routing.py"
    )

    spelled = {"five": 5, "six": 6, "seven": 7}
    for page, pattern in (
        ("concepts.mdx", r"of (five|six|seven) values"),
        ("provisioning-scenarios.mdx", r"one of (five|six|seven) values the schema enforces"),
        ("schema-reference.mdx", r"All (five|six|seven) live in"),
    ):
        assert spelled[figure(page, pattern)] == len(defined), f"{page} disagrees with routing.py"


def test_the_monitor_reading_arithmetic_is_the_schema() -> None:
    """`concepts.mdx` and `schema-reference.mdx` both count the monitor readings and
    both say how many families share one.

    Both said the only shared reading was `measured_gain_mdb`. `OtnChannelMonitor`
    contributes `total_power_mdbm` and `channel_count` to a ROADM degree and a
    multiplexer, so three readings are shared and not one. The "eleven of
    fourteen" arithmetic on the same page was right the whole time and is what
    proves the sentence wrong.
    """
    parsed = yaml.safe_load((SCHEMA_DIR / "otn_ports.yml").read_text())
    generics = {generic["name"]: generic for generic in parsed.get("generics", [])}

    def readings(node: dict) -> set[str]:
        """Stored readings only: no `_display` mirror, and not the timestamp."""
        names: set[str] = set()
        sources = [node] + [generics[parent[3:]] for parent in node.get("inherit_from", []) if parent[3:] in generics]
        for source in sources:
            for attribute in source.get("attributes", []):
                name = attribute["name"]
                if not name.endswith("_display") and name != "measured_at":
                    names.add(name)
        return names

    families = {node["name"]: readings(node) for node in parsed.get("nodes", []) if node["name"].endswith("Monitor")}
    counts: dict[str, int] = {}
    for owned in families.values():
        for name in owned:
            counts[name] = counts.get(name, 0) + 1

    total = len(counts)
    alone = sum(1 for count in counts.values() if count == 1)
    shared = total - alone

    spelled = {"one": 1, "three": 3, "eleven": 11, "fourteen": 14}
    for page, total_pattern, alone_pattern in (
        ("concepts.mdx", r"of the (fourteen) in the model,\n(eleven)", None),
        ("schema-reference.mdx", r"\n(fourteen) readings in the model, (eleven)", None),
    ):
        found = re.findall(total_pattern, doc_text(page))
        assert len(found) == 1, f"{page}: the reading arithmetic moved"
        assert spelled[found[0][0]] == total, f"{page} says {found[0][0]} readings, the schema has {total}"
        assert spelled[found[0][1]] == alone, f"{page} says {found[0][1]} on one family, the schema has {alone}"

    for page in ("concepts.mdx", "schema-reference.mdx"):
        said = figure(page, r"(One|Three) readings are shared")
        assert spelled[said.lower()] == shared, f"{page} says {said} shared, the schema shares {shared}"


# ---------------------------------------------------------------------------
# What the demo runs
# ---------------------------------------------------------------------------


def test_the_scenario_count_the_demo_guide_publishes_is_what_the_tasks_number() -> None:
    """`demo-guide.mdx` opens with the scenario count and lists one row each.

    It said eight and listed eight while `tasks.py` numbered nine, because
    `demo-infiniband` carries "Scenario 9" in its docstring and ran inside the
    walkthrough without appearing in the guide.
    """
    numbered = {int(number) for number in re.findall(r"\"\"\"Scenario (\d+):", (REPO_ROOT / "tasks.py").read_text())}
    assert numbered == set(range(1, len(numbered) + 1)), f"the scenario numbers have a gap: {sorted(numbered)}"

    spelled = {"Eight": 8, "Nine": 9, "Ten": 10}
    said = spelled[figure("demo-guide.mdx", r"(Eight|Nine|Ten) scenarios against")]
    assert said == len(numbered), f"the guide says {said} scenarios, tasks.py numbers {len(numbered)}"

    rows = re.findall(r"^\| (\d+) \| ", doc_text("demo-guide.mdx"), re.MULTILINE)
    assert [int(row) for row in rows] == sorted(numbered), "the guide's table is missing a numbered scenario"


# ---------------------------------------------------------------------------
# What the pages tell a reader to run
# ---------------------------------------------------------------------------

GUARDED_PAGES: tuple[Path, ...] = (*sorted(DOC_DIR.glob("*.mdx")), REPO_ROOT / "README.md")

ILLUSTRATION_LANGUAGES = ("jinja", "mermaid", "yaml", "python", "json", "text")
"""Fence tags that show a reader what something looks like rather than what to type.

Everything else is read as commands: `bash`, `sh`, `shell` and `console`, but
also an untagged block and a tag nobody has classified yet. The riskier reading
is the right default for a guard, and it is what stops a raw command being hidden
behind a fence tag instead of being moved into a task.
"""

ALLOWED_PREFIXES = {
    "uv run invoke": "The task surface. Everything a reader types should be one of these.",
    "git clone": "Fetches the repository, so it necessarily runs before any task exists.",
    "cd ": "Changes into the clone, in the same block as the `git clone`.",
    "cp .env.example": "Writes the file the API token lives in. No task can read a token before it.",
    "uv sync": "Installs invoke itself, so it too runs before any task exists.",
    "node scripts/extract_basemap.mjs": (
        "Regenerating `basemap.py`. Needs Node and a Natural Earth release you downloaded, "
        "runs by hand when the window or the source changes, and has no task behind it. "
        "`developer-guide.mdx` says so in the prose beside it."
    ),
}
"""Every command prefix a page may hold, each with the reason it is excused.

The four setup prefixes are all one shape: a reader cannot run a task until they
have the repository, the dependencies and the token. The basemap line is the one
genuine exception, and it is named rather than disguised. Retagging its fence as
`text` would hide it from this test and is what makes a guard rot.

`data-model.md` also allowed the shell keywords, for a `for` loop on
`loadable-scenarios.mdx` that the rewrite removed. Nothing on any page needs them
now, and an allowance nothing uses is a hole rather than a convenience.
"""


@dataclass(frozen=True)
class CommandBlock:
    """One fenced block, with the absolute line of each command it holds."""

    page: str
    line: int
    language: str
    commands: tuple[str, ...]
    numbers: tuple[int, ...]


@dataclass(frozen=True)
class DocumentedCommand:
    """One `uv run invoke ...` a page tells a reader to run."""

    page: str
    line: int
    task: str
    options: tuple[str, ...]
    values: dict[str, str]


def _command(line: str) -> str:
    """One block line as a command, or empty for a blank, a comment or a prompt."""
    text = line.strip().removeprefix("$ ").strip()
    return "" if text.startswith("#") else text


@cache
def _parse(path: Path) -> tuple[tuple[CommandBlock, ...], str]:
    """Every fenced block on one page, and the page with those blocks blanked out.

    Blanking rather than deleting keeps the line numbers, and it stops the inline
    scan below pairing one block's closing fence with the next block's opening one.
    """
    blocks: list[CommandBlock] = []
    prose: list[str] = []
    opening: tuple[int, str] | None = None
    body: list[tuple[int, str]] = []

    for number, line in enumerate(path.read_text().splitlines(), 1):
        fence = re.match(r"^```(\S*)\s*$", line)
        if opening is None and fence:
            opening, body = (number, fence.group(1)), []
        elif opening is not None and line.startswith("```"):
            kept = [(at, _command(text)) for at, text in body]
            blocks.append(
                CommandBlock(
                    page=path.name,
                    line=opening[0],
                    language=opening[1],
                    commands=tuple(text for _, text in kept if text),
                    numbers=tuple(at for at, text in kept if text),
                )
            )
            opening = None
        elif opening is not None:
            body.append((number, line))
        else:
            prose.append(line)
            continue
        prose.append("")

    return tuple(blocks), "\n".join(prose)


def _fenced_blocks() -> tuple[CommandBlock, ...]:
    return tuple(block for path in GUARDED_PAGES for block in _parse(path)[0])


def _is_command_block(block: CommandBlock) -> bool:
    return block.language not in ILLUSTRATION_LANGUAGES


LONG_OPTION = "--"
"""Written as a constant so the option parsing below is not read as name parsing.

`test_repository_config.py` forbids taking a hyphenated string apart, because a
device name is an identifier and nothing may recover meaning from it. A command
line is the one place where splitting on a hyphen is the whole job, and spelling
the marker out keeps the two apart without an exemption.
"""


def _invocation(page: str, line: int, command: str) -> DocumentedCommand | None:
    """One command line as a `DocumentedCommand`, or None when it runs no task."""
    if not command.startswith("uv run invoke "):
        return None
    words = command.removeprefix("uv run invoke ").split("#", 1)[0].split()
    if not words:
        return None

    task, rest = words[0], words[1:]
    options: list[str] = []
    values: dict[str, str] = {}
    index = 0
    while index < len(rest):
        word = rest[index]
        index += 1
        if not word.startswith(LONG_OPTION):
            continue
        name = word[len(LONG_OPTION) :].replace("-", "_")
        options.append(name)
        if index < len(rest) and not rest[index].startswith(LONG_OPTION):
            values[name] = rest[index]
            index += 1
    return DocumentedCommand(page=page, line=line, task=task, options=tuple(options), values=values)


def _documented_commands() -> tuple[DocumentedCommand, ...]:
    """Every documented invocation, from the command blocks and from the prose.

    The prose half is not decoration. `developer-guide.mdx` names `invoke build`
    and `invoke start --rebuild` only inside a sentence, and a rename would leave
    those two wrong with every block still passing.
    """
    found: list[DocumentedCommand | None] = []
    for block in _fenced_blocks():
        if _is_command_block(block):
            found += [_invocation(block.page, at, text) for at, text in zip(block.numbers, block.commands, strict=True)]
    for path in GUARDED_PAGES:
        prose = _parse(path)[1]
        for match in re.finditer(r"`([^`]+)`", prose, re.DOTALL):
            spanned = " ".join(match.group(1).split())
            found.append(_invocation(path.name, prose[: match.start()].count("\n") + 1, spanned))
    return tuple(command for command in found if command is not None)


def test_every_command_a_page_runs_is_on_the_allow_list() -> None:
    """A reader following a page types the task surface and almost nothing else.

    The allow list above names every exception and why it is one. A new command
    on a page is either a task or a new named entry; it is never a quiet addition.
    """
    offenders = []
    for block in _fenced_blocks():
        if not _is_command_block(block):
            continue
        for at, command in zip(block.numbers, block.commands, strict=True):
            if not any(command.startswith(prefix) for prefix in ALLOWED_PREFIXES):
                offenders.append(f"{block.page}:{at} runs `{command}`")

    assert not offenders, (
        "commands the pages run that ALLOWED_PREFIXES in this module does not permit: "
        + "; ".join(offenders)
        + ". Move the work into an invoke task, or add a prefix with the reason it is excused: allowed today are "
        + ", ".join(f"`{prefix.strip()}`" for prefix in ALLOWED_PREFIXES)
    )


def test_no_page_carries_a_fenced_graphql_block() -> None:
    """A reader is never asked to paste GraphQL. Every query the demo runs is stored.

    `queries/` holds them and a task or a check names the one it wants, so a query
    on a page is a copy that nothing keeps in step.
    """
    pages = {path.name for path in DOC_DIR.glob("*.mdx")}
    offenders = [
        f"{block.page}:{block.line}"
        for block in _fenced_blocks()
        if block.language == "graphql" and block.page in pages
    ]
    assert not offenders, (
        "fenced graphql blocks under docs/docs/: "
        + "; ".join(offenders)
        + ". Store the query in queries/ and point a task or a check at it."
    )


def test_every_documented_command_matches_the_task_it_runs() -> None:
    """Every documented invocation is checked against the task's real signature.

    Invoke derives its options from the function parameters, so the collection is
    the authority and no second list is kept here. It replaces the older test that
    compared the task name alone and passed on an option no task accepts.
    """
    collection = Collection.from_module(tasks)
    known = set(collection.task_names)
    offenders = []

    for command in _documented_commands():
        where = f"{command.page}:{command.line} `uv run invoke {command.task}`"
        if command.task not in known:
            offenders.append(f"{where} is not a task in tasks.py")
            continue
        accepted = {argument.name: argument.kind for argument in collection[command.task].get_arguments()}
        for option in command.options:
            if option not in accepted:
                offenders.append(f"{where} passes --{option.replace('_', '-')}, which the task does not accept")
            elif accepted[option] is bool and option in command.values:
                offenders.append(f"{where} gives --{option.replace('_', '-')} a value, and it is a flag")
            elif accepted[option] is not bool and option not in command.values:
                offenders.append(f"{where} passes --{option.replace('_', '-')} with no value")

    assert not offenders, "documented commands that do not match tasks.py: " + "; ".join(sorted(offenders))


def test_every_task_in_the_default_listing_is_documented() -> None:
    """The other direction: a reader-facing task cannot ship undocumented.

    `invoke list` prints one set and `--all` prints the rest. Everything in the
    first set is something a reader is meant to run, so a page has to say so.
    """
    listed = {name for _, names, _ in tasks.TASK_GROUPS for name in names}
    documented = {command.task for command in _documented_commands()}
    missing = sorted(listed - documented)

    assert not missing, (
        "tasks `uv run invoke list` shows that no page tells a reader to run: "
        + ", ".join(missing)
        + ". Document each on a page, or move it into the hidden half of TASK_GROUPS in tasks.py."
    )


STATED_DEFAULTS = (
    ("loadable-scenarios.mdx", r"It makes `(\S+)` if it is not there, loads both files", "demo-odu", "branch"),
    ("loadable-scenarios.mdx", r"It makes `(\S+)`, loads both files, provisions all four", "demo-diversity", "branch"),
    ("loadable-scenarios.mdx", r"Every step below runs on the `(\S+)` branch", "demo", "branch"),
    ("quickstart.mdx", r"It makes the `(\S+)` branch if it is not there", "demo-provision", "branch"),
)
"""Every sentence on a page that names a value a task supplies for itself.

All four say which branch a task lands on when the reader passes none, which is
the only kind of default the pages state today. There is no wider
convention to generalise over, and a pattern matched against nothing would pass
while proving nothing, so the sentences are pinned one at a time. `figure` fails
if one of them is reworded, which is the point: a reworded sentence is exactly
where a stated default goes stale.
"""


def test_a_default_a_page_states_is_the_default_the_task_carries() -> None:
    """Where a page saves the reader an argument, it names the value the task uses."""
    collection = Collection.from_module(tasks)
    assert STATED_DEFAULTS, "a stated-defaults table with no rows would pass while proving nothing"

    for page, pattern, task, option in STATED_DEFAULTS:
        stated = figure(page, pattern)
        text = doc_text(page)
        at = text[: re.search(pattern, text).start()].count("\n") + 1  # type: ignore[union-attr]
        defaults = {argument.name: argument.default for argument in collection[task].get_arguments()}
        assert stated == defaults[option], (
            f"{page}:{at} says `uv run invoke {task}` uses {stated!r}, "
            f"tasks.py defaults {option} to {defaults[option]!r}"
        )


# ---------------------------------------------------------------------------
# What the pipeline registers
# ---------------------------------------------------------------------------

COUNTS = {"three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
"""The window of spellings the check-count patterns below accept.

Wide enough that adding a check produces a failure naming the old word, rather
than a pattern that matches nothing and a message about a missing regex. It runs
down to three because the same table reads the split between the checks that
speak about one change and the checks that stay quiet.
"""


def test_the_check_count_every_page_publishes_is_what_infrahub_yml_registers() -> None:
    """Four pages state how many checks the repository ships, in seven sentences.

    Nothing read `.infrahub.yml` until this test did. Feature 024 registered
    `channel_count_consistency` and `monitor_completeness`, taking the repository
    from six checks to eight, and every one of those seven sentences stayed on
    six with the whole suite green. That is the same drift this module was
    written for, arriving in the one figure it had not been pointed at.

    The enumeration on `client-mapping.mdx` is held to the register as well, not
    only the count. A page can say eight and list six.
    """
    registered = [entry["name"] for entry in REPOSITORY_CONFIG["check_definitions"]]

    for page, pattern in (
        ("client-mapping.mdx", r"repository ships (\w+) checks"),
        ("developer-guide.mdx", r"creates (\w+) check definitions"),
        ("provisioning-scenarios.mdx", r"runs the (\w+) checks"),
        ("provisioning-scenarios.mdx", r"of the (\w+) checks say something"),
        ("what-this-shows.mdx", r"whole pipeline: (\w+) checks"),
        ("what-this-shows.mdx", r"registers (\w+) check definitions"),
        ("README.md", r"\*\*Block a bad merge\.\*\* (\w+) checks run"),
    ):
        # Lowered because README.md's count opens a sentence and the others do not.
        said = figure(page, pattern)
        assert COUNTS.get(said.lower()) == len(registered), (
            f"{page} says {said} checks against {pattern!r}, .infrahub.yml registers {len(registered)}"
        )

    listed = set(re.findall(r"`(\w+)`", figure("client-mapping.mdx", r"repository ships \w+ checks: ([^.]+)\.")))
    assert listed == set(registered), f"client-mapping.mdx lists {sorted(listed)}, .infrahub.yml registers {registered}"

    named = figure("README.md", r"- \*\*Checks\.\*\* ([^.]+)\.")
    assert len(re.findall(r",| and ", named)) + 1 == len(registered), (
        f"README.md's checks bullet names {named!r} against {len(registered)} registered checks"
    )


def test_the_provisioning_page_accounts_for_every_check_that_ran() -> None:
    """The page splits the checks into the ones that speak and the ones that do not.

    Two spelled numbers and a bulleted list, and all three have to agree with the
    register. The split moved when `channel_count_consistency` was added: a
    provisioning branch lights a wavelength and updates no monitor reading, so the
    check has something to say about scenario one and the numerator moved with the
    denominator. A page that had only corrected six to eight would have left three
    against eight and lost four findings a reader is about to see.

    Speaking is not the same as refusing, and this test does not ask which. The
    four `channel_count_consistency` findings on that branch are INFO: a monitor
    reading is dated and can only ever under-report a design newer than itself, so
    a degree that has not yet seen the new wavelength is reported and does not
    block. The page has to name the check either way, because a check that logs
    four lines and is missing from the list is four lines a reader cannot place.
    """
    registered = REPOSITORY_CONFIG["check_definitions"]
    page = doc_text("provisioning-scenarios.mdx")

    speaking = COUNTS[figure("provisioning-scenarios.mdx", r"(\w+) of the \w+ checks say something").lower()]
    silent = COUNTS[figure("provisioning-scenarios.mdx", r"The other (\w+) say nothing").lower()]
    assert speaking + silent == len(registered), (
        f"the page accounts for {speaking} plus {silent} of {len(registered)} checks"
    )

    block = page.split("checks say something about this change:", 1)[1].split("say nothing", 1)[0]
    named = re.findall(r"^- `(\w+)`", block, re.MULTILINE)
    assert len(named) == speaking, f"the page says {speaking} checks speak and bullets {len(named)}: {named}"
    assert set(named) <= {entry["name"] for entry in registered}, f"bulleted checks that are not registered: {named}"


def test_the_validator_count_the_provisioning_page_publishes_is_the_pipeline_plus_the_built_ins() -> None:
    """One number on `provisioning-scenarios.mdx`, and it is the only place a
    reader is told what a full pipeline looks like.

    Every definition in `.infrahub.yml` becomes one validator on a proposed
    change, and four more run that nothing in the file asks for. The page said
    fourteen, which was measured live and correct for six checks. Two more checks
    make sixteen.
    """
    expected = (
        len(REPOSITORY_CONFIG["check_definitions"])
        + len(REPOSITORY_CONFIG["generator_definitions"])
        + len(REPOSITORY_CONFIG["artifact_definitions"])
        + BUILT_IN_VALIDATORS
    )
    spelled = {"fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18}
    said = spelled[figure("provisioning-scenarios.mdx", r"from two validators to (\w+)")]
    assert said == expected, f"the page says {said} validators, the repository registers {expected}"


def test_the_stored_query_count_the_pages_publish_is_what_queries_holds() -> None:
    """Two pages count the stored queries, and both were left on seventeen when
    feature 024 added two `.gql` files.

    The count is the directory, not `.infrahub.yml`: `queries/` is loaded whole
    and a query is bound by the Python class that names it, so a file that no
    definition mentions is still a stored query on the server.
    """
    actual = len(list((REPO_ROOT / "queries").glob("*.gql")))
    spelled = {"seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20}
    for page, pattern in (
        ("developer-guide.mdx", r"one generator definition and (\w+)\nGraphQL queries"),
        ("what-this-shows.mdx", r"\| (\w+) queries, one per report or check"),
    ):
        said = spelled[figure(page, pattern).lower()]
        assert said == actual, f"{page} says {said} queries, queries/ holds {actual}"


def test_the_monitor_coverage_line_the_scenario_page_quotes_is_the_dataset() -> None:
    """`loadable-scenarios.mdx` quotes the coverage summary the completeness check
    prints on the branch holding `demo/10_amplifier_without_monitor.yml`.

    Five pairs of figures, every one of them a count of shipped objects, and the
    amplifier denominator is the only one the scenario file moves. A seed change
    that adds a Raman pump moves a pair the page would otherwise keep quoting.
    """
    covered = MANIFEST["OtnAmplifier"]
    added = len(demo_objects_of_kind("10_amplifier_without_monitor.yml", "OtnAmplifier"))
    expected = (
        (covered, covered + added),
        (MANIFEST["OtnRamanPump"], MANIFEST["OtnRamanPump"]),
        (MANIFEST["OtnTransponder"], MANIFEST["OtnTransponder"]),
        (MANIFEST["OtnMuxDemux"], MANIFEST["OtnMuxDemux"]),
        (MANIFEST["OtnRoadmDegreePort"], MANIFEST["OtnRoadmDegreePort"]),
    )

    quoted = figure(
        "loadable-scenarios.mdx",
        r"Monitor coverage: (\d+/\d+ amplifiers,\s+\d+/\d+ Raman pumps,\s+\d+/\d+ transponders,"
        r"\s+\d+/\d+ multiplexers,\s+\d+/\d+ ROADM degrees)\.",
    )
    pairs = tuple((int(left), int(right)) for left, right in re.findall(r"(\d+)/(\d+)", quoted))
    assert pairs == expected, f"the page quotes {pairs}, the dataset and the scenario give {expected}"

    clean = figure("loadable-scenarios.mdx", r"first figure reads (\d+/\d+)")
    assert clean == f"{covered}/{covered}", f"the page says the default branch reads {clean}"


# ---------------------------------------------------------------------------
# What the maps draw
# ---------------------------------------------------------------------------


def test_the_odu_band_table_is_the_renderers_band_edges() -> None:
    """`odu-map.mdx` tabulates the colour bands a reader sees in the legend.

    The edges are client sizes, so a change to the slot table moves both the map
    and this table. The page's reading of the middle band claimed a 40G fits
    anywhere in it, which is false below 32 free slots.
    """
    page = doc_text("odu-map.mdx")
    low, middle, high = HEADROOM_BAND_EDGES_SLOTS

    assert re.search(rf"\| Green \| {high} or more \|", page), f"the green band starts at {high} slots"
    assert re.search(rf"\| Blue \| {middle} to {high - 1} \|", page), f"the blue band is {middle} to {high - 1}"
    assert re.search(rf"\| Amber \| {low} to {middle - 1} \|", page), f"the amber band is {low} to {middle - 1}"


def test_the_slot_sizes_the_odu_map_page_names_are_the_slot_table() -> None:
    """The same page reads its band edges out in container names.

    A 40G client is the reason `ODU3` has to be named here: it sits inside the
    blue band and needs four times the `ODU2` the page used to credit it to.
    """
    page = doc_text("odu-map.mdx")
    spelled = {"Eight": 8, "Thirty-two": 32, "Eighty": 80}
    for word, container in (("Eight", "ODU2"), ("Thirty-two", "ODU3"), ("Eighty", "ODU4")):
        assert re.search(rf"{word} (?:slots )?is an `{container}`", page), f"{container} is no longer named"
        assert spelled[word] == SLOT_TABLE[container].offers, f"{container} does not offer {spelled[word]} slots"


def test_the_system_margin_the_network_map_page_states_is_the_one_applied() -> None:
    """`network-map.mdx` explains what the route colour measures.

    It named the mode requirement and stopped there, so a reader recomputing the
    Paris to Madrid deficit by hand landed a decibel out.
    """
    stated = float(figure("network-map.mdx", r"adds ([\d.]+) dB of system margin"))
    assert stated == mdb_to_db(SYSTEM_MARGIN_MDB), (
        f"the page states {stated} dB, budget.py applies {mdb_to_db(SYSTEM_MARGIN_MDB)}"
    )


def test_the_artifact_copy_count_is_the_pop_count() -> None:
    """Both map pages tell a reader how many copies of each artifact exist.

    One per PoP, and the customer campus is not a PoP. `transforms/odu_map.py`
    said fifteen in its own docstring while its sibling said fourteen.
    """
    pops = sum(1 for site in objects_of_kind("OtnSite") if site.get("site_type") != "customer")
    spelled = {"thirteen": 13, "fourteen": 14, "fifteen": 15}

    said = spelled[figure("odu-map.mdx", r"One copy per PoP, (thirteen|fourteen|fifteen) in all")]
    assert said == pops, f"odu-map.mdx says {said} copies, the dataset has {pops} PoPs"

    for transform in ("odu_map.py", "network_map.py"):
        text = (REPO_ROOT / "transforms" / transform).read_text()
        for word in re.findall(r"fails all (thirteen|fourteen|fifteen) artifacts", text):
            assert spelled[word] == pops, f"transforms/{transform} says {word} artifacts, there are {pops} PoPs"


def test_the_published_map_legends_are_the_renderers_captions() -> None:
    """`docs/docs/media/*.svg` are the pictures a reader actually looks at.

    These two are rendered from a live branch and copied in by hand, so no test
    and no gate connects them to the renderer that produced them. That is how
    the ODU map came to publish "a 10G or 40G, not a 100G" for a week after the
    band's caption was corrected: the prose, the four goldens and the renderer
    all agreed, and the picture on the page still told the reader a 40G fits.

    Only the legend captions are pinned. Everything else on those maps depends
    on which branch and which scenario was loaded when they were rendered, and a
    test that demanded a byte match would fail on a re-render that changed
    nothing a reader would notice.
    """
    published = (REPO_ROOT / "docs" / "docs" / "media" / "odu-map.svg").read_text()
    low, middle, high = HEADROOM_BAND_EDGES_SLOTS

    for band, expected_range in (
        (HEADROOM_BANDS[0], f"fewer than {low} free"),
        (HEADROOM_BANDS[1], f"{low} to {middle - 1} free"),
        (HEADROOM_BANDS[2], f"{middle} to {high - 1} free"),
        (HEADROOM_BANDS[3], f"{high} or more free"),
    ):
        line = f"{expected_range}: {band.caption}"
        assert line in published, f"odu-map.svg does not carry the legend line {line!r}; re-render it"


def test_no_page_anywhere_states_a_check_count_that_is_not_the_register() -> None:
    """The sweep behind the enumerated assertions above.

    That test names six sentences on four pages, which is every place anyone had
    found. `quickstart.mdx` had a seventh, and it survived feature 024 with the
    whole suite green: the line wraps between the number and the noun, so both a
    grep for "six checks" and a per-page regex anchored on one line missed it.

    So this one takes the opposite approach and looks for the shape rather than
    the sentence. Any spelled number followed by "check" or "checks" across
    whitespace, on any page, has to be the number of definitions `.infrahub.yml`
    registers. A page that grows an eighth sentence is covered the day it is
    written rather than the day someone remembers to add it here.
    """
    registered = len(REPOSITORY_CONFIG["check_definitions"])
    offenders = []

    for path in sorted(DOC_DIR.glob("*.mdx")):
        text = path.read_text()
        for match in re.finditer(r"\b([a-z]+)\s+checks?\b", text):
            spelled = COUNTS.get(match.group(1))
            if spelled is None or spelled == registered:
                continue
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.name}:{line} says {match.group(1)} where the register holds {registered}")

    assert not offenders, "check counts that disagree with .infrahub.yml: " + "; ".join(offenders)
