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


# ---------------------------------------------------------------------------
# What the schema is
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# What the demo runs
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The figures the pages publish
# ---------------------------------------------------------------------------

SPELLED: dict[str, int] = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
}
"""Words a page may write a figure as instead of digits.

The pages spell small numbers and print large ones, which is ordinary English and
not something a test should force either way. Both readings are accepted and the
comparison happens on the integer.
"""

LEDGER_DEVICE_KINDS = (
    "OtnRouter",
    "OtnTransponder",
    "OtnRoadm",
    "OtnAmplifier",
    "OtnMuxDemux",
    "OtnPatchPanel",
    "OtnRamanPump",
    "OtnOduSwitch",
)
"""What `installation-setup.mdx` means by a device: everything a rack holds."""

LEDGER_PORT_KINDS = (
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
"""What the same sentence means by a port, monitors included.

A monitor is a port on the modelled device and is counted as one on the page, so
it is counted as one here.
"""

FACT_MARKER = re.compile(r'<span data-fact="([a-z-]+)">([^<]+)</span>')
"""How a page marks a figure this module owns.

A plain `span` rather than an MDX component, so it needs no import on the page,
no theme swizzle and no build step. Docusaurus renders it as its text and GitHub
renders `README.md` the same way, so a reader sees the number and nothing else.
"""


def _monitor_reading_counts() -> dict[str, int]:
    """How many monitor families own each stored reading.

    A `_display` mirror is a rendering of a reading, not a second one, and
    `measured_at` is the timestamp every family carries.
    """
    parsed = yaml.safe_load((SCHEMA_DIR / "otn_ports.yml").read_text())
    generics = {generic["name"]: generic for generic in parsed.get("generics", [])}

    def readings(node: dict) -> set[str]:
        names: set[str] = set()
        sources = [node] + [generics[parent[3:]] for parent in node.get("inherit_from", []) if parent[3:] in generics]
        for source in sources:
            for attribute in source.get("attributes", []):
                name = attribute["name"]
                if not name.endswith("_display") and name != "measured_at":
                    names.add(name)
        return names

    counts: dict[str, int] = {}
    for node in parsed.get("nodes", []):
        if node["name"].endswith("Monitor"):
            for name in readings(node):
                counts[name] = counts.get(name, 0) + 1
    return counts


def _bulleted_speaking_checks() -> list[str]:
    """The checks `provisioning-scenarios.mdx` bullets as speaking about the change.

    Bounded by the two fact markers rather than by the sentences around them.
    Anchoring on the prose is what this module is being cured of, and it bit here
    first: the block used to be found by splitting on "checks say something about
    this change:", and rewrapping that sentence to fit the line length put a
    newline inside the phrase and took three tests down with an `IndexError`.

    The markers cannot drift the same way. They are what the test owns, a
    reader never sees them, and no line-length rule will ever break one.
    """
    page = doc_text("provisioning-scenarios.mdx")
    opens = page.find('<span data-fact="checks-speaking">')
    closes = page.find('<span data-fact="checks-silent">')
    assert 0 <= opens < closes, "the speaking and silent markers are not both on the page, in that order"
    return re.findall(r"^- `(\w+)`", page[opens:closes], re.MULTILINE)


@cache
def facts() -> dict[str, int]:
    """Every figure a page is allowed to state, each derived rather than typed.

    One entry per claim, and the entry is the computation. A page states the
    figure inside a `data-fact` span and this is what the span is checked
    against, so a page may be reworded freely and a figure may not be wrong.
    """
    collection = Collection.from_module(tasks)
    listed = {name for _, names, _ in tasks.TASK_GROUPS for name in names}
    devices = yaml.safe_load((SCHEMA_DIR / "otn_devices.yml").read_text())
    routing = (REPO_ROOT / "src" / "infrahub_demo_otn" / "routing.py").read_text()
    readings = _monitor_reading_counts()
    scenarios = {int(number) for number in re.findall(r'"""Scenario (\d+):', (REPO_ROOT / "tasks.py").read_text())}
    assert scenarios == set(range(1, len(scenarios) + 1)), f"the scenario numbers have a gap: {sorted(scenarios)}"

    return {
        # What the load is. Every figure but the first is read out of
        # `scripts/geant_manifest.json`, the ledger the seed generator writes,
        # rather than counted out of `objects/` a second time here. The two
        # agreed when this moved, and one of them is already guarded: the
        # generator regenerates the ledger and `test_geant_dataset.py` diffs it,
        # so a seed change that moved a count and left the ledger alone fails
        # there before it reaches a page.
        #
        # `objects` is the exception and has to be. It is the whole load, and the
        # ledger covers the generated seed only: the catalogs, the modes and the
        # client signals are hand-written files it never sees. 2344 against the
        # ledger's 2204 is those files, not a disagreement.
        "objects": sum(len((document.get("spec") or {}).get("data") or []) for document in object_documents()),
        "optical-elements": sum(MANIFEST[kind] for kind in OPTICAL_ELEMENT_KINDS),
        "sites": MANIFEST["OtnSite"],
        "sections": MANIFEST["OtnOpticalMultiplexSection"],
        "spans": MANIFEST["OtnFiberSpan"],
        "devices": sum(MANIFEST[kind] for kind in LEDGER_DEVICE_KINDS),
        "ports": sum(MANIFEST[kind] for kind in LEDGER_PORT_KINDS),
        "wavelengths": MANIFEST["OtnOpticalCarrier"],
        # What the schema is.
        "device-kinds": sum(
            1 for node in devices.get("nodes", []) if "OtnGenericDevice" in (node.get("inherit_from") or [])
        ),
        "rejection-codes": len(set(re.findall(r'^REASON_([A-Z_]+) = "', routing, re.MULTILINE))),
        "monitor-readings": len(readings),
        "monitor-readings-alone": sum(1 for count in readings.values() if count == 1),
        "monitor-readings-shared": sum(1 for count in readings.values() if count > 1),
        # What the demo runs.
        "scenarios": len(scenarios),
        "checks": len(REPOSITORY_CONFIG["check_definitions"]),
        "validators": (
            len(REPOSITORY_CONFIG["check_definitions"])
            + len(REPOSITORY_CONFIG["generator_definitions"])
            + len(REPOSITORY_CONFIG["artifact_definitions"])
            + BUILT_IN_VALIDATORS
        ),
        "queries": len(list((REPO_ROOT / "queries").glob("*.gql"))),
        # The split between the checks that speak about one change and the ones
        # that stay quiet. Derived from the bulleted list on the page rather than
        # from the register, because which checks speak is a property of the
        # branch and only the page knows it. The bullets are structure; the two
        # sentences quoting them are prose, and this is what holds them together.
        "checks-speaking": len(_bulleted_speaking_checks()),
        "checks-silent": len(REPOSITORY_CONFIG["check_definitions"]) - len(_bulleted_speaking_checks()),
        # What a reader types.
        "tasks-defined": len(collection.task_names),
        "tasks-listed": len(listed),
        "tasks-hidden": len(set(collection.task_names) - listed),
    }


def _stated_facts() -> list[tuple[str, int, str, int]]:
    """Every `data-fact` span on every guarded page, as (name, line, page, value).

    `value` is the integer the page states, spelled or in digits, with any
    thousands comma removed.
    """
    found = []
    for path in GUARDED_PAGES:
        if not path.exists():
            continue
        text = path.read_text()
        for match in FACT_MARKER.finditer(text):
            name, written = match.group(1), match.group(2).strip()
            line = text[: match.start()].count("\n") + 1
            bare = written.replace(",", "").lower()
            value = int(bare) if bare.isdigit() else SPELLED.get(bare, -1)
            found.append((name, line, path.name, value))
    return found


def test_every_figure_a_page_states_is_the_figure_the_repository_has() -> None:
    """Every `data-fact` span holds the number its name computes to.

    This replaced fifteen tests that each pulled a figure out of prose with a
    regex. Those failed two ways and could not tell them apart: rewording a
    sentence failed with `expected one match ... found 0` while the page was
    still correct, and moving a figure failed with a truncated dict comparison
    that named no field. The loud failure was the wrong one.

    Marking the figure inverts it. The page says which claim a number is and the
    prose around it is free, so the only way to fail is to be wrong.
    """
    known = facts()
    wrong = []
    for name, line, page, value in _stated_facts():
        where = f"{page}:{line}"
        if name not in known:
            wrong.append(f"{where} states `{name}`, which is not a fact this module computes")
        elif value < 0:
            wrong.append(f"{where} states `{name}` as a word this module cannot read as a number")
        elif value != known[name]:
            wrong.append(f"{where} says {name} is {value}, the repository has {known[name]}")

    assert not wrong, "; ".join(wrong)


def test_every_figure_this_module_computes_is_stated_somewhere() -> None:
    """The other direction, so the registry cannot outlive the sentence.

    A fact nobody states is a computation kept in step with nothing, which is the
    shape the deleted `schema-vendor-diff` task had: correct, maintained, and
    read by no one.
    """
    stated = {name for name, _, _, _ in _stated_facts()}
    orphaned = sorted(set(facts()) - stated)
    assert not orphaned, f"facts computed here that no page states: {orphaned}"


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
    registered = {entry["name"] for entry in REPOSITORY_CONFIG["check_definitions"]}
    named = _bulleted_speaking_checks()

    assert named, "the page bullets no speaking check, so this test reads nothing"
    unregistered = sorted(set(named) - registered)
    assert not unregistered, f"bulleted checks that .infrahub.yml does not register: {unregistered}"

    known = facts()
    assert known["checks-speaking"] + known["checks-silent"] == known["checks"], (
        f"the page accounts for {known['checks-speaking']} plus {known['checks-silent']} of {known['checks']} checks"
    )


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
