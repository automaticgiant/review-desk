import json

import pytest

from conftest import make_packet
from review_desk.registry import Registry, RegistryError


def test_add_list_remove(registry, packet, state_dir):
    e = registry.add(packet)
    assert e.path == str(packet.resolve())
    assert registry.get(e.id).path == e.path
    assert [x.id for x in registry.entries()] == [e.id]
    # persisted, under the state dir
    data = json.loads((state_dir / "packets.json").read_text())
    assert data["packets"][0]["path"] == e.path
    # a fresh instance reads the same file
    assert Registry.default().get(e.id).path == e.path
    assert registry.remove(packet) is True
    assert registry.entries() == []
    assert registry.remove(packet) is False


def test_add_is_idempotent_and_ids_stable(registry, packet):
    a = registry.add(packet)
    b = registry.add(str(packet) + "/")
    assert a.id == b.id and len(registry.entries()) == 1


def test_ids_distinct_for_same_dir_name(registry, tmp_path):
    a = registry.add(make_packet(tmp_path / "x" / "drafts"))
    b = registry.add(make_packet(tmp_path / "y" / "drafts"))
    assert a.id != b.id
    assert all(c.isalnum() or c == "-" for c in a.id)


def test_add_requires_valid_manifest(registry, tmp_path):
    with pytest.raises(RegistryError):
        registry.add(tmp_path)  # no review.toml
    assert registry.entries() == []


def test_corrupt_registry_is_an_error_not_a_wipe(registry, packet, state_dir):
    registry.add(packet)
    (state_dir / "packets.json").write_text("{not json")
    with pytest.raises(RegistryError):
        Registry.default().entries()
    assert (state_dir / "packets.json").read_text() == "{not json"
