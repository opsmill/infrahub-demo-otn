"""Every reference resolves against something already loaded when it is read."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from tests.unit.conftest import DEMO_DIR, demo_objects_of_kind, object_files
from tests.unit.scenariopayloads import merged, scenario_files, schema

Record = dict[str, Any]
Key = tuple[str, ...]


def _key(kind: str, record: Record) -> Key:
    """A record's human-friendly ID.

    A kind the schema does not declare is keyed on `name` alone, which is what
    every Infrahub built-in an object file here creates uses.
    """
    if kind not in schema():
        return (str(record.get("name", "")),)
    return tuple(str(record.get(part.split("__")[0], "")) for part in schema()[kind].hfid)


def _references(kind: str, record: Record, known: set[tuple[str, Key]]) -> list[tuple[str, Any]]:
    """The relationship values on one record that name nothing declared yet."""
    unresolved: list[tuple[str, Any]] = []
    for field in schema()[kind].relationships.values():
        if field.name not in record:
            continue
        value = record[field.name]
        if value is None:
            continue
        candidates = _peer_kinds(field.peer)
        raw = value if isinstance(value, list) else [value]

        flat = tuple(str(part) for part in raw if not isinstance(part, list))
        if flat and any((peer, flat) in known for peer in candidates):
            continue
        if flat and any(flat in _all_keys(peer) for peer in candidates):
            unresolved.append((field.name, value))
            continue

        for item in raw:
            parts = tuple(str(part) for part in item) if isinstance(item, list) else (str(item),)
            if any((peer, parts) in known for peer in candidates):
                continue
            unresolved.append((field.name, parts[0] if len(parts) == 1 else list(parts)))
    return unresolved


def _peer_kinds(peer: str) -> tuple[str, ...]:
    """The kinds a relationship to `peer` can land on."""
    if peer in schema():
        return (peer,)
    inheriting = tuple(name for name in schema() if peer in _inherits(name))
    return inheriting or (peer,)


def _inherits(kind: str) -> tuple[str, ...]:
    return _INHERITS.get(kind, ())


def _build_inherits() -> dict[str, tuple[str, ...]]:
    from tests.unit.conftest import schema_files

    collected: dict[str, tuple[str, ...]] = {}
    for path in schema_files():
        document = yaml.safe_load(path.read_text()) or {}
        for entry in document.get("nodes") or []:
            name = str(entry["namespace"]) + str(entry["name"])
            collected[name] = tuple(str(item) for item in entry.get("inherit_from") or [])
    return collected


_INHERITS = _build_inherits()


def _all_keys(kind: str) -> set[Key]:
    """Every record of one kind in the shipped dataset, keyed as the loader sees it.

    Read straight out of the files rather than out of `merged(None)`, because the
    merged view drops kinds the schema does not declare and this test does not.
    """
    if kind in schema():
        return set(merged(None).get(kind, {}))
    return {
        (str(record.get("name", "")),)
        for path in object_files()
        for document in _documents(path.read_text())
        if str((document.get("spec") or {}).get("kind")) == kind
        for record in (document.get("spec") or {}).get("data") or []
        if isinstance(record, dict)
    }


def _documents(text: str) -> list[Record]:
    return [parsed for parsed in yaml.safe_load_all(text) if isinstance(parsed, dict)]


def _walk(paths: list[Any], known: set[tuple[str, Key]]) -> list[str]:
    """Insert every record in load order, reporting each forward reference."""
    complaints: list[str] = []
    for path in paths:
        for document in _documents(path.read_text()):
            spec = document.get("spec") or {}
            kind = str(spec.get("kind") or "")
            if not kind:
                continue
            for record in spec.get("data") or []:
                if not isinstance(record, dict):
                    continue
                # A kind the schema does not declare has no relationships this
                # test can read, so it contributes a declaration and no
                # references. That is the whole of what a BuiltinTag record is
                # to the load order: something a later file can name.
                references = _references(kind, record, known) if kind in schema() else []
                for field, value in references:
                    complaints.append(
                        f"{path.name}: {kind} {record.get('name')!r} names {value!r} on `{field}`, "
                        "which nothing has declared at that point in the load order"
                    )
                known.add((kind, _key(kind, record)))
    return complaints


def test_the_shipped_dataset_never_names_an_object_it_has_not_loaded_yet() -> None:
    """`objects/` read in the order the loader reads it."""
    complaints = _walk(object_files(), set())
    assert not complaints, "\n".join(complaints)


@pytest.mark.parametrize("file_name", scenario_files())
def test_each_scenario_never_names_an_object_it_has_not_loaded_yet(file_name: str) -> None:
    """One scenario file, over a branch that already holds the shipped dataset."""
    known = {(kind, key) for kind, records in merged(None).items() for key in records}
    complaints = _walk([DEMO_DIR / file_name], known)
    assert not complaints, "\n".join(complaints)


def test_a_scenario_that_adds_a_device_declares_it_before_its_ports() -> None:
    """The specific shape that failed, held on its own so the message names it."""
    for file_name in scenario_files():
        added = {str(record["name"]) for record in demo_objects_of_kind(file_name, "OtnTransponder")}
        added |= {str(record["name"]) for record in demo_objects_of_kind(file_name, "OtnOduSwitch")}
        if not added:
            continue
        ports = demo_objects_of_kind(file_name, "OtnLinePort")
        hung = {str(record["device"]) for record in ports} & added
        if not hung:
            continue
        order = [
            str((document.get("spec") or {}).get("kind")) for document in _documents((DEMO_DIR / file_name).read_text())
        ]
        for device_kind in ("OtnTransponder", "OtnOduSwitch"):
            if device_kind in order and "OtnLinePort" in order:
                assert order.index(device_kind) < order.index("OtnLinePort"), (
                    f"{file_name} declares its line ports before the {device_kind} they sit on"
                )
