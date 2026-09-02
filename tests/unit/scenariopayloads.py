"""A branch, assembled offline, in the shape a check's stored query returns it."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path
from typing import Any

import yaml

from tests.unit.conftest import DEMO_DIR, REPO_ROOT, SCHEMA_DIR, object_documents

QUERY_DIR = REPO_ROOT / "queries"

Record = dict[str, Any]
Key = tuple[str, ...]
View = dict[str, dict[Key, Record]]
Index = dict[tuple[str, str], dict[tuple[str, Key], list[tuple[str, Key]]]]
"""(identifier, field name) -> (kind, id) -> the peers that field returns."""


# ---------------------------------------------------------------------------
# The schema, flattened
# ---------------------------------------------------------------------------


class Field:
    """One relationship, as much of it as resolving needs."""

    __slots__ = ("cardinality", "identifier", "name", "peer")

    def __init__(self, name: str, peer: str, cardinality: str, identifier: str) -> None:
        self.name = name
        self.peer = peer
        self.cardinality = cardinality
        self.identifier = identifier


class Kind:
    __slots__ = ("defaults", "hfid", "name", "relationships")

    def __init__(self, name: str, hfid: tuple[str, ...], defaults: dict[str, Any], relationships: dict[str, Field]):
        self.name = name
        self.hfid = hfid
        self.defaults = defaults
        self.relationships = relationships


@cache
def schema() -> dict[str, Kind]:
    """Every concrete kind, with what it inherits folded in."""
    raw_generics: dict[str, Record] = {}
    raw_nodes: dict[str, Record] = {}
    for path in sorted(SCHEMA_DIR.glob("*.yml")):
        document = yaml.safe_load(path.read_text()) or {}
        for entry in document.get("generics") or []:
            raw_generics[str(entry["namespace"]) + str(entry["name"])] = entry
        for entry in document.get("nodes") or []:
            raw_nodes[str(entry["namespace"]) + str(entry["name"])] = entry

    kinds: dict[str, Kind] = {}
    for name, node in raw_nodes.items():
        sources = [raw_generics[parent] for parent in node.get("inherit_from") or [] if parent in raw_generics]
        sources.append(node)
        # An object file writes an attribute only where it differs from the
        # default the schema declares, so a record that says nothing about
        # `status` still arrives as `active` on the server. A resolver that
        # returned null there would hand every check a state no branch holds,
        # and `carrier_termination` would read forty carriers as out of scope.
        defaults: dict[str, Any] = {}
        relationships: dict[str, Field] = {}
        hfid: tuple[str, ...] = ()
        for source in sources:
            for item in source.get("attributes") or []:
                defaults[str(item["name"])] = item.get("default_value")
            for item in source.get("relationships") or []:
                relationships[str(item["name"])] = Field(
                    str(item["name"]),
                    str(item["peer"]),
                    str(item.get("cardinality") or "many"),
                    str(item.get("identifier") or ""),
                )
            declared = source.get("human_friendly_id")
            if declared:
                hfid = tuple(str(part) for part in declared)
        kinds[name] = Kind(name, hfid or ("name__value",), defaults, relationships)
    return kinds


@cache
def _concrete(peer: str) -> tuple[str, ...]:
    """The kinds a relationship to `peer` can land on."""
    if peer in schema():
        return (peer,)
    raw: list[str] = []
    for path in sorted(SCHEMA_DIR.glob("*.yml")):
        document = yaml.safe_load(path.read_text()) or {}
        for entry in document.get("nodes") or []:
            if peer in (entry.get("inherit_from") or []):
                raw.append(str(entry["namespace"]) + str(entry["name"]))
    return tuple(sorted(raw))


def _key(kind: str, record: Record) -> Key:
    """A record's human-friendly ID, as the tuple the schema declares."""
    parts: list[str] = []
    for part in schema()[kind].hfid:
        field = part.split("__")[0]
        parts.append(str(record.get(field, "")))
    return tuple(parts)


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------


def _documents(path: Path) -> list[Record]:
    return [parsed for parsed in yaml.safe_load_all(path.read_text()) if isinstance(parsed, dict)]


def _apply(view: View, documents: list[Record]) -> None:
    for document in documents:
        spec = document.get("spec") or {}
        kind = str(spec.get("kind") or "")
        if kind not in schema():
            continue
        for record in spec.get("data") or []:
            if not isinstance(record, dict):
                continue
            bucket = view.setdefault(kind, {})
            key = _key(kind, record)
            bucket[key] = {**bucket.get(key, {}), **record}


@cache
def shipped() -> View:
    """The default branch: `objects/` and nothing else."""
    view: View = {}
    _apply(view, list(object_documents()))
    return view


@cache
def merged(file_name: str | None = None) -> View:
    """The default branch with one scenario file loaded over it.

    `None` is the default branch itself, which is what the sweep compares a
    scenario against and what the resolver's own agreement test runs on.
    """
    view: View = {kind: dict(records) for kind, records in shipped().items()}
    if file_name is not None:
        _apply(view, _documents(DEMO_DIR / file_name))
    return view


def scenario_files() -> tuple[str, ...]:
    """Every file under `demo/`, discovered rather than listed.

    Listed is how a thirteenth scenario ships unswept.
    """
    return tuple(sorted(path.name for path in DEMO_DIR.glob("*.yml")))


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


@cache
def edges(file_name: str | None = None) -> Index:
    """The edge index for one merged view, built once per view."""
    return _build_edges(merged(file_name))


@cache
def _inverse_name(peer: str, identifier: str, forward: str) -> str | None:
    """What the far side calls the same edge, when it calls it anything."""
    for candidate in _concrete(peer):
        for field in schema()[candidate].relationships.values():
            if field.identifier == identifier and field.name != forward:
                return field.name
    return None


def _build_edges(view: View) -> Index:
    """(identifier, field name) -> (kind, id) -> the peers that field returns."""
    index: Index = {}
    for kind, records in view.items():
        for key, record in records.items():
            for field in schema()[kind].relationships.values():
                if field.name not in record:
                    continue
                candidates = _concrete(field.peer)
                inverse = _inverse_name(field.peer, field.identifier, field.name)
                for raw in _resolve_peers(view, record[field.name], candidates):
                    sides = [((field.identifier, field.name), (kind, key), raw)]
                    if inverse is not None:
                        sides.append(((field.identifier, inverse), raw, (kind, key)))
                    for bucket_key, side, peer in sides:
                        reachable = index.setdefault(bucket_key, {}).setdefault(side, [])
                        if peer not in reachable:
                            reachable.append(peer)
    return index


def _resolve_peers(view: View, value: Any, candidates: tuple[str, ...]) -> list[tuple[str, Key]]:
    """Match a relationship value against the records it could name."""
    found: list[tuple[str, Key]] = []
    if value is None:
        return found
    raw = value if isinstance(value, list) else [value]

    composite = [str(part) for part in raw if not isinstance(part, list)]
    if composite:
        for kind in candidates:
            whole = tuple(composite)
            if whole in view.get(kind, {}):
                return [(kind, whole)]
    for item in raw:
        parts = tuple(str(part) for part in item) if isinstance(item, list) else (str(item),)
        for kind in candidates:
            if parts in view.get(kind, {}):
                found.append((kind, parts))
                break
    return found


# ---------------------------------------------------------------------------
# The query
# ---------------------------------------------------------------------------

_TOKEN = re.compile(r"\.\.\.\s+on\s+(\w+)|(\w+)\s*(?:\([^)]*\))?|([{}])")


def _parse(text: str) -> dict[str, Any]:
    """A stored query as a nested selection tree."""
    stripped = re.sub(r"#[^\n]*", "", text)
    for arguments in re.findall(r"\(([^)]*)\)", stripped):
        named = {name for name in re.findall(r"(\w+)\s*:", arguments)}
        if named - {"limit"}:
            raise ValueError(
                f"this resolver reads unfiltered queries only, and this one takes {sorted(named - {'limit'})}"
            )

    root: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [root]
    pending: str | None = None
    for match in _TOKEN.finditer(stripped):
        fragment, name, brace = match.groups()
        if fragment:
            # Flush first. A fragment nearly always follows a leaf, because a
            # query that discriminates on kind selects `__typename` next to the
            # fragment so the reader can tell which branch it got. Overwriting
            # the pending field here dropped that `__typename` from every
            # selection that had one, and `monitor_completeness` then read 390
            # devices as carrying no monitor at all.
            if pending is not None:
                stack[-1][pending] = {}
            pending = f"... on {fragment}"
        elif name:
            if name == "query":
                pending = None
                continue
            if pending is not None:
                stack[-1][pending] = {}
            pending = name
        elif brace == "{":
            if pending is None:
                continue
            child: dict[str, Any] = {}
            stack[-1][pending] = child
            stack.append(child)
            pending = None
        elif brace == "}":
            if pending is not None:
                stack[-1][pending] = {}
                pending = None
            stack.pop()
    if pending is not None:
        stack[-1][pending] = {}
    return next(iter(root.values())) if len(root) == 1 and not root.get("edges") else root


def _strip(fields: dict[str, Any]) -> dict[str, Any]:
    """Drop the `edges` and `node` wrappers, keeping the fields under them."""
    while set(fields) in ({"edges"}, {"node"}):
        fields = next(iter(fields.values()))
    return {name: _strip(children) for name, children in fields.items()}


@cache
def selection(query_name: str) -> dict[str, Any]:
    return _strip(_parse((QUERY_DIR / f"{query_name}.gql").read_text()))


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------


def _node(
    view: View,
    index: Index,
    kind: str,
    key: Key,
    fields: dict[str, Any],
) -> Record:
    record = view[kind][key]
    node: Record = {}
    for field, children in fields.items():
        if field.startswith("... on "):
            if field[len("... on ") :] == kind:
                node.update(_node(view, index, kind, key, children))
            continue
        if field == "id":
            node["id"] = "/".join(key)
            continue
        if field == "__typename":
            node["__typename"] = kind
            continue
        relationship = schema()[kind].relationships.get(field)
        if relationship is not None:
            peers = index.get((relationship.identifier, relationship.name), {}).get((kind, key), [])
            allowed = set(_concrete(relationship.peer))
            peers = [peer for peer in peers if peer[0] in allowed]
            if relationship.cardinality == "one":
                node[field] = {"node": _node(view, index, *peers[0], children) if peers else None}
            else:
                node[field] = {
                    "edges": [
                        {"node": _node(view, index, peer_kind, peer_key, children)} for peer_kind, peer_key in peers
                    ]
                }
            continue
        node[field] = {"value": record.get(field, schema()[kind].defaults.get(field))}
    return node


def payload(query_name: str, file_name: str | None = None) -> Record:
    """One branch in the shape one stored query returns it.

    `file_name` is a scenario under `demo/`, or `None` for the default branch.
    """
    view = merged(file_name)
    index = edges(file_name)
    result: Record = {}
    for kind, fields in selection(query_name).items():
        if kind not in schema():
            result[kind] = {"edges": []}
            continue
        result[kind] = {
            "edges": [{"node": _node(view, index, kind, key, fields)} for key in sorted(view.get(kind, {}))]
        }
    return result
