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
    """A record's human-friendly ID."""
    if kind not in schema():
        return (str(record.get("name", "")),)
    return tuple(str(record.get(part.split("__")[0], "")) for part in schema()[kind].hfid)


def _named_kind(value: Any) -> tuple[str, Record] | None:
    """The `{kind, data}` form a reference takes, or `None` for a plain one.

    `OtnTransceiver.port` peers `OtnOpticalPort`, and that generic carries no
    `human_friendly_id`: the identity keys live on `OtnGenericPort`, a sibling
    generic rather than a parent. So the loader has nothing to resolve a bare
    `[device, name]` pair against and refuses it. The reference names the
    concrete kind and the loader resolves the fields against that instead.
    """
    if not isinstance(value, dict) or "kind" not in value:
        return None
    data = value.get("data")
    return (str(value["kind"]), data) if isinstance(data, dict) else None


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

        # A reference that names its own kind is read against that kind alone.
        # It takes this form because its peer is a generic with no identity
        # keys, so the loader has nothing to resolve a bare pair against, and
        # the fields it carries are named rather than positional.
        named = [item for item in (_named_kind(item) for item in raw) if item is not None]
        if named:
            for kind_name, data in named:
                parts = _key(kind_name, data)
                if kind_name in candidates and (kind_name, parts) in known:
                    continue
                unresolved.append((field.name, list(parts)))
            continue

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
    """Every record of one kind in the shipped dataset, keyed as the loader sees it."""
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


def test_the_optics_load_after_the_catalog_and_after_the_ports_they_sit_in() -> None:
    """Where the two transceiver files sit, held on its own so a rename says so.

    Load order is filename order, and `14a_geant_transceivers.yml` sorts after
    `14_geant_ports.yml` only because an underscore sorts before a letter. A
    fitted unit names its port and its part number and the loader resolves both
    at insert time, so a filename that sorted the other way would fail the whole
    batch on the server rather than here. The test above finds the same fault
    from the reference side; this one names the file that moved.
    """
    order = [path.name for path in object_files()]
    for earlier, later in (
        ("03_optical_modes.yml", "06_transceiver_types.yml"),
        ("06_transceiver_types.yml", "14a_geant_transceivers.yml"),
        ("14_geant_ports.yml", "14a_geant_transceivers.yml"),
        ("14a_geant_transceivers.yml", "15_geant_spans.yml"),
    ):
        assert order.index(earlier) < order.index(later), f"{later} loads before {earlier}"


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
