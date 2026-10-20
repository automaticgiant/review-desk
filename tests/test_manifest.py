import pytest

from conftest import MANIFEST, make_packet
from review_desk.manifest import ManifestError, load


def test_parses_packet_and_items(packet):
    p = load(packet)
    assert p.title == "Demo packet"
    assert p.intro.startswith("Two drafts")
    assert [(l.label, l.url) for l in p.links] == [("Ticket", "https://example.com/T-1")]
    assert [i.id for i in p.items] == ["first-draft", "second-draft"]
    first, second = p.items
    assert first.kind == "new Task" and first.recommendation == "**File** it"
    assert [(l.label, l.url) for l in first.links] == [("https://example.com/a", "https://example.com/a")]
    assert second.facts == {"Overlaps": "[issue 1](https://example.com/issues/1)"}
    assert p.file_path(second) == (packet / "sub/second.md").resolve()
    assert p.item("second-draft") is second and p.item("nope") is None


def test_missing_manifest(tmp_path):
    with pytest.raises(ManifestError, match="review.toml"):
        load(tmp_path)


def test_bad_toml(tmp_path):
    make_packet(tmp_path, "title = ")
    with pytest.raises(ManifestError, match="TOML"):
        load(tmp_path)


@pytest.mark.parametrize("bad, msg", [
    ('[[items]]\nid="a"\ntitle="x"\n', "title"),                                      # packet title missing
    ('title="t"\n[[items]]\nid="Bad Id"\ntitle="x"\n', "id"),                           # id not a slug
    ('title="t"\n[[items]]\nid="a"\ntitle="x"\n[[items]]\nid="a"\ntitle="y"\n', "duplicate"),
    ('title="t"\n[[items]]\nid="a"\n', "title"),                                        # item title missing
    ('title="t"\n[[items]]\nid="a"\ntitle="x"\nfile="../escape.md"\n', "outside"),      # traversal
    ('title="t"\n[[items]]\nid="a"\ntitle="x"\nfile="/etc/passwd"\n', "relative"),     # absolute
    ('title="t"\n[[items]]\nid="a"\ntitle="x"\nfile="x.html"\n', ".md"),               # wrong type
    ('title="t"\n[[items]]\nid="a"\ntitle="x"\nlinks=[{url="javascript:alert(1)"}]\n', "http"),
    ('title="t"\nitems="nope"\n', "items"),
])
def test_rejects_invalid(tmp_path, bad, msg):
    make_packet(tmp_path, bad, files={})
    with pytest.raises(ManifestError, match=msg):
        load(tmp_path)


def test_symlink_escape_rejected(tmp_path):
    outside = tmp_path / "secret.md"
    outside.write_text("secret")
    pkt = make_packet(tmp_path / "p", 'title="t"\n[[items]]\nid="a"\ntitle="x"\nfile="link.md"\n', files={})
    (pkt / "link.md").symlink_to(outside)
    with pytest.raises(ManifestError, match="outside"):
        load(pkt)


def test_missing_file_is_allowed_but_reported(tmp_path):
    p = load(make_packet(tmp_path, MANIFEST, files={}))
    assert p.file_path(p.items[0]) is not None
    assert not p.file_path(p.items[0]).exists()
