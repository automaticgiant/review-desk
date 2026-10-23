import http.client
import json
import threading
from pathlib import Path

import pytest

MANIFEST = '''
title = "Demo packet"
intro = "Two drafts for **testing**."
links = [{label = "Ticket", url = "https://example.com/T-1"}]

[[items]]
id = "first-draft"
title = "First draft"
kind = "new Task"
recommendation = "**File** it"
file = "first-draft.md"
links = ["https://example.com/a"]

[[items]]
id = "second-draft"
title = "Second draft"
kind = "comment"
recommendation = "Send it"
file = "sub/second.md"
[items.facts]
Overlaps = "[issue 1](https://example.com/issues/1)"
'''


def make_packet(root: Path, manifest: str = MANIFEST, files=None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "review.toml").write_text(manifest)
    files = files if files is not None else {"first-draft.md": "# First\n\nbody one", "sub/second.md": "# Second\n\nbody two"}
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


@pytest.fixture
def packet(tmp_path):
    return make_packet(tmp_path / "pkt")


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("REVIEW_DESK_STATE", str(d))
    return d


@pytest.fixture
def registry(state_dir):
    from review_desk.registry import Registry
    return Registry.default()


class Client:
    def __init__(self, port):
        self.port = port

    def request(self, method, path, body=None, headers=None, host=None, raw_body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{self.port}"}
        data = raw_body
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if data is not None:
            h["Content-Length"] = str(len(data))
        h.update(headers or {})
        conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in h.items():
            conn.putheader(k, v)
        conn.endheaders()
        if data is not None:
            conn.send(data)
        r = conn.getresponse()
        out = r.status, dict(r.getheaders()), r.read()
        conn.close()
        return out

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def put_item(self, pid, item, body, headers=None, **kw):
        h = {"X-Review-Desk": "1"}
        h.update(headers or {})
        return self.request("PUT", f"/api/p/{pid}/items/{item}", body=body, headers=h, **kw)


@pytest.fixture
def desk(registry, packet):
    from review_desk.server import make_server
    entry = registry.add(packet)
    srv = make_server(0, registry)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    t.start()
    client = Client(srv.server_address[1])
    client.pid = entry.id
    client.packet = packet
    client.registry = registry
    client.srv = srv
    yield client
    srv.shutdown()
    srv.server_close()
