"""Every reference resolves against something already loaded when it is read.

**What this is for.** `infrahubctl object load` resolves a human-friendly
identifier at insert time and makes no deferred second pass, so a record naming
an object that has not been inserted yet fails, and it takes the whole batch with
it. The order is the sorted file names, then the documents inside each file, then
the records inside each document, which is why the object files carry numeric
prefixes.

That rule was already known and already written down. `carriers_with_line_ports`
in the dataset generator explains why the carrier writes `line_ports` rather than
the port writing `carrier`, and `19_geant_odu_switches.yml` explains why the O-E-O
devices load last. What did not exist was anything that checked it. Feature 026
put two line ports on `oeo-fra-01` into `14_geant_ports.yml`, which loads five
files before the device is created, and every unit test passed. The integration
suite caught it half an hour later, after a container stack, a schema load and
2344 objects.

So the rule is a two-second test now. It reads the same files in the same order
the loader does and asks, of every reference, whether the thing it names has been
declared yet.

**`demo/` too, on the same terms and one file at a time.** A scenario file is
loaded onto a branch that already holds `objects/`, so its references may reach
back into the shipped dataset freely and forward into nothing. Several scenario
files contradict each other, so no two are ever considered together.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from tests.unit.conftest import DEMO_DIR, demo_objects_of_kind, object_files
from tests.unit.scenariopayloads import merged, scenario_files, schema

Record = dict[str, Any]
Key = tuple[str, ...]


def _key(kind: str, record: Record) -> Key:
    return tuple(str(record.get(part.split("__")[0], "")) for part in schema()[kind].hfid)


def _references(kind: str, record: Record, known: set[tuple[str, Key]]) -> list[tuple[str, Any]]:
    """The relationship values on one record that name nothing declared yet.

    A value is interpreted against the whole view rather than parsed, because a
    composite identifier and a list of simple ones look the same in YAML:
    `connected_to: [roadm-ams-01, AD-01]` is one peer of two parts and
    `sections: [oms-a, oms-b]` is two peers of one part. Whichever reading finds
    records is the reading the loader will take.
    """
    unresolved: list[tuple[str, Any]] = []
    for field in schema()[kind].relationships.values():
        if field.name not in record:
            continue
        value = record[field.name]
        if value is None:
            continue
        candidates = _peer_kinds(field.peer)
        if not candidates:
            # A peer this repository does not declare is an Infrahub built-in,
            # `BuiltinTag` on `OtnSite.tags` being the one that occurs. The
            # server resolves those against kinds no file here creates, so this
            # test has nothing to say about them and says nothing rather than
            # reporting six sites for naming tags that exist.
            continue
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
    if peer in schema():
        return (peer,)
    return tuple(name for name in schema() if peer in _inherits(name))


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
    return set(merged(None).get(kind, {}))


def _documents(text: str) -> list[Record]:
    return [parsed for parsed in yaml.safe_load_all(text) if isinstance(parsed, dict)]


def _walk(paths: list[Any], known: set[tuple[str, Key]]) -> list[str]:
    """Insert every record in load order, reporting each forward reference."""
    complaints: list[str] = []
    for path in paths:
        for document in _documents(path.read_text()):
            spec = document.get("spec") or {}
            kind = str(spec.get("kind") or "")
            if kind not in schema():
                continue
            for record in spec.get("data") or []:
                if not isinstance(record, dict):
                    continue
                for field, value in _references(kind, record, known):
                    complaints.append(
                        f"{path.name}: {kind} {record.get('name')!r} names {value!r} on `{field}`, "
                        "which nothing has declared at that point in the load order"
                    )
                known.add((kind, _key(kind, record)))
    return complaints


def test_the_shipped_dataset_never_names_an_object_it_has_not_loaded_yet() -> None:
    """`objects/` read in the order the loader reads it.

    The numeric prefixes are the load order and they are load-bearing. Three
    files carry a comment saying which way an edge had to be written to satisfy
    this, and until now none of the three was checked.
    """
    complaints = _walk(object_files(), set())
    assert not complaints, "\n".join(complaints)


@pytest.mark.parametrize("file_name", scenario_files())
def test_each_scenario_never_names_an_object_it_has_not_loaded_yet(file_name: str) -> None:
    """One scenario file, over a branch that already holds the shipped dataset.

    Parametrised per file rather than looped, because scenario files are
    alternatives: `demo/06_mad_waw_16qam.yml` and `demo/07_mad_waw_qpsk.yml`
    describe two branches and not one state, and merging them would assert
    against a network that never exists.
    """
    known = {(kind, key) for kind, records in merged(None).items() for key in records}
    complaints = _walk([DEMO_DIR / file_name], known)
    assert not complaints, "\n".join(complaints)


def test_a_scenario_that_adds_a_device_declares_it_before_its_ports() -> None:
    """The specific shape that failed, held on its own so the message names it.

    A port is a component of a device and a device is created by whichever file
    creates it. `demo/04_odu_ten_in_one.yml` and `demo/90_fra_mil_saturated.yml`
    each rack a shelf and then hang two line ports off it, and
    `objects/19_geant_odu_switches.yml` does the same for the regenerator. All
    three only work because the device's document comes first.
    """
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
