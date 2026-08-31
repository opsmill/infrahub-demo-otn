"""Hold the sidebar to its shape, offline.

Three failures this module exists to catch, all of them silent on a running
server.

A menu entry naming a kind that does not exist drops that branch of the sidebar
and reports nothing. A sidebar that grows one entry per schema kind buries the
five roots an operator navigates by. And a trim that removes an entry for a kind
nothing else reaches makes those objects unreachable from the UI entirely.

The third one is the reason this module resolves inheritance before it computes
anything. `OtnRoadm.ports` is not declared on `OtnRoadm`: it comes from
`OtnGenericDevice` through `inherit_from`, and the same is true of every device
and port kind. A relationship whose peer is a generic reaches every kind that
inherits it. Without both steps this would fail on kinds that are perfectly
reachable, which is the fastest way to have a test deleted.
"""

from functools import cache
from typing import Any

import yaml

from tests.unit.conftest import REPO_ROOT, schema_files

MENU_FILE = REPO_ROOT / "menus" / "otn.yml"

MAX_TOP_LEVEL = 5
MAX_LEAVES = 13

PORT_KINDS = {
    "OtnRouterPort",
    "OtnClientPort",
    "OtnLinePort",
    "OtnRoadmAddDropPort",
    "OtnRoadmDegreePort",
    "OtnAmplifierPort",
    "OtnTributaryPort",
}
DEVICE_KINDS = {
    "OtnRouter",
    "OtnTransponder",
    "OtnRoadm",
    "OtnAmplifier",
    "OtnMuxDemux",
    "OtnPatchPanel",
    "OtnRamanPump",
    "OtnOduSwitch",
}
"""Reached from a site page and a device page. A sidebar entry for either is a
second route to a place the operator was already going to arrive at.
`OtnRamanPump` is reached from its span as well, through `raman_pumps`, and
`OtnOduSwitch` from its wavelength, through `odu_switches`."""

CORE_KINDS = {"CoreArtifact", "CoreGeneratorGroup", "CoreStandardGroup", "CoreProposedChange"}
"""Kinds Infrahub ships. They are not in `schemas/` and are still valid `kind:`
values for a menu entry."""


# ---------------------------------------------------------------------------
# The menu file
# ---------------------------------------------------------------------------
@cache
def _menu() -> tuple[dict[str, Any], ...]:
    parsed = yaml.safe_load(MENU_FILE.read_text())
    assert isinstance(parsed, dict), f"{MENU_FILE.name} does not parse to a mapping"
    assert parsed.get("apiVersion") == "infrahub.app/v1", "menu file is missing apiVersion: infrahub.app/v1"
    assert parsed.get("kind") == "Menu", "menu file is missing kind: Menu"
    data = (parsed.get("spec") or {}).get("data")
    assert isinstance(data, list) and data, "menu file declares no spec.data"
    return tuple(data)


def _walk(items: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every item in the tree, parents included."""
    found: list[dict[str, Any]] = []
    for item in items:
        found.append(item)
        children = (item.get("children") or {}).get("data") or []
        found.extend(_walk(children))
    return found


def _leaves() -> list[dict[str, Any]]:
    """Every item carrying a kind. A group header carries none."""
    return [item for item in _walk(_menu()) if item.get("kind")]


def _menu_kinds() -> set[str]:
    return {str(item["kind"]) for item in _leaves()}


# ---------------------------------------------------------------------------
# The schema, with inheritance resolved
# ---------------------------------------------------------------------------
@cache
def _schema() -> tuple[dict[str, list[str]], dict[str, list[dict[str, Any]]], dict[str, set[str]]]:
    """(inherit_from per kind, relationships per kind, members per generic)."""
    inherits: dict[str, list[str]] = {}
    relationships: dict[str, list[dict[str, Any]]] = {}
    generics: set[str] = set()

    for path in schema_files():
        document = yaml.safe_load(path.read_text())
        assert isinstance(document, dict), f"{path.name} does not parse to a mapping"
        for section in ("generics", "nodes"):
            for entry in document.get(section) or []:
                kind = f"{entry.get('namespace', '')}{entry.get('name', '')}"
                if section == "generics":
                    generics.add(kind)
                inherits[kind] = list(entry.get("inherit_from") or [])
                relationships.setdefault(kind, []).extend(entry.get("relationships") or [])

    members = {generic: {kind for kind, parents in inherits.items() if generic in parents} for generic in generics}
    return inherits, relationships, members


def _all_kinds() -> set[str]:
    inherits, _, _ = _schema()
    return set(inherits)


def _relationships_of(kind: str) -> list[dict[str, Any]]:
    """Declared plus inherited, so a ROADM reports the ports it gets from the
    device generic."""
    inherits, relationships, _ = _schema()
    collected = list(relationships.get(kind) or [])
    for parent in inherits.get(kind) or []:
        collected.extend(relationships.get(parent) or [])
    return collected


def _reaches(kind: str) -> set[str]:
    """Every kind one click from `kind`'s detail page.

    A relationship whose peer is a generic reaches every kind that inherits it:
    a site's `devices` list holds routers and ROADMs, not `OtnGenericDevice`
    objects.
    """
    _, _, members = _schema()
    found: set[str] = set()
    for relationship in _relationships_of(kind):
        peer = str(relationship.get("peer") or "")
        if not peer:
            continue
        found.add(peer)
        found |= members.get(peer, set())
    return found


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_no_menu_item_is_silently_dropped() -> None:
    """Two declaration faults the server accepts and never reports.

    Two siblings sharing a namespace and name collapse into one entry, and an
    item declaring both a kind and a path makes Infrahub fall through to the
    auto-generated menu for that branch. A missing namespace or name is not
    checked here: the loader rejects that one loudly.
    """
    seen: set[tuple[str, str]] = set()
    for item in _walk(_menu()):
        key = (str(item.get("namespace")), str(item.get("name")))
        assert key not in seen, f"two menu items share namespace/name {key}, so one of them disappears"
        seen.add(key)

    offenders = [str(item["name"]) for item in _walk(_menu()) if item.get("kind") and item.get("path")]
    assert not offenders, f"menu items declaring both kind and path: {offenders}"


def test_every_kind_the_menu_names_exists() -> None:
    """A menu entry naming a kind that does not exist drops that branch of the
    sidebar and reports nothing."""
    unknown = sorted(_menu_kinds() - _all_kinds() - CORE_KINDS)
    assert not unknown, f"the menu names kinds that are neither in schemas/ nor core: {unknown}"


def test_every_kind_the_menu_names_is_hidden_from_the_automatic_menu() -> None:
    """Custom menus and the auto-generated sidebar are additive. A kind in both
    appears twice, which reads as the custom menu being broken."""
    visible: list[str] = []
    for path in schema_files():
        document = yaml.safe_load(path.read_text())
        for section in ("generics", "nodes"):
            for entry in document.get(section) or []:
                kind = f"{entry.get('namespace', '')}{entry.get('name', '')}"
                if kind in _menu_kinds() and entry.get("include_in_menu") is not False:
                    visible.append(kind)
    assert not visible, f"kinds in the custom menu without include_in_menu: false: {sorted(visible)}"


def test_the_sidebar_keeps_the_shape_an_operator_can_navigate() -> None:
    """One design intent, stated once.

    A sidebar an operator navigates by is a handful of roots holding a short
    list of leaves. It fails in three directions: back to the single root it
    started as, out to a flat list of every kind, or by naming a device or port
    kind that the site and device pages already reach. The bounds are ceilings
    rather than equalities, because removing an entry is always allowed and
    adding one is the move that walks the sidebar back to 29 leaves.
    """
    top_level, leaves, named = _menu(), _leaves(), _menu_kinds()
    assert len(top_level) > 1, "the sidebar is a single root again"
    assert len(top_level) <= MAX_TOP_LEVEL, f"{len(top_level)} top-level entries, at most {MAX_TOP_LEVEL}"
    assert len(leaves) <= MAX_LEAVES, (
        f"{len(leaves)} sidebar leaves, at most {MAX_LEAVES}: {sorted(str(item['kind']) for item in leaves)}"
    )
    assert not named & PORT_KINDS, f"the sidebar names port kinds again: {sorted(named & PORT_KINDS)}"
    assert not named & DEVICE_KINDS, f"the sidebar names device kinds again: {sorted(named & DEVICE_KINDS)}"


def test_every_kind_absent_from_the_sidebar_is_reachable_from_one_that_is_present() -> None:
    """The check that makes the trim safe.

    Removing an entry is only correct when an object page still reaches the
    kind. Generics are excluded from the requirement: a generic is not an object
    an operator opens, it is how several kinds are listed at once.
    """
    _, _, members = _schema()
    generics = set(members)
    present = _menu_kinds()
    reachable: set[str] = set(present)
    for kind in present:
        reachable |= _reaches(kind)
    # One more hop. A container is reached from a carrier, which is in the
    # sidebar; a path hop is reached from an optical path, which is reached from
    # a service. Two hops is still navigation; more is a treasure hunt.
    for kind in list(reachable):
        reachable |= _reaches(kind)

    orphans = sorted(_all_kinds() - generics - reachable)
    assert not orphans, (
        f"kinds with no sidebar entry and no relationship from one: {orphans}. "
        "Either put them back in menus/otn.yml or give the peer that should list them an inverse."
    )
