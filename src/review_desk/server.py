"""The desk: one loopback HTTP server for every registered packet.

Routes (anything else is 404):
  GET /                                  index of registered packets
  GET /p/<pid>/                          packet page, rendered live from review.toml and the draft files
  GET /p/<pid>/raw/<item_id>             an item's draft file as text/plain (only files the manifest names)
  GET /api/health                        {"app": "review-desk"}
  GET /api/p/<pid>/state                 the packet's review-decisions.json
  PUT /api/p/<pid>/items/<item_id>       merge {"decision"?, "note"?} into one item
  GET /static/page.css|page.js

Security rules (each has a test in tests/test_server.py):
  - bind 127.0.0.1 only;
  - Host must be exactly 127.0.0.1:<port> or localhost:<port> (DNS rebinding), on every request;
  - writes need X-Review-Desk: 1 (a cross-origin form or no-cors fetch can't set it), JSON content type,
    and a same-origin Origin when one is sent;
  - only registered packets, item ids from the manifest, and manifest-validated files are reachable;
    path segments must match [a-z0-9-]+ with no decoding, so encoded traversal never routes;
  - request bodies are capped at MAX_BODY;
  - pages escape untrusted text (render.py) and carry a strict CSP.
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import urlsplit

from . import decisions as dec
from . import render
from .manifest import ManifestError, load
from .registry import Registry, RegistryError
from .summary import summarize_entry

MAX_BODY = 64 * 1024
DRAIN_LIMIT = 16 * 1024 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
SEG = r"([a-z0-9][a-z0-9-]{0,127})"
ROUTES_GET = [
    (re.compile(r"^/$"), "index"),
    (re.compile(rf"^/p/{SEG}/$"), "packet"),
    (re.compile(rf"^/p/{SEG}/raw/{SEG}$"), "raw"),
    (re.compile(r"^/api/health$"), "health"),
    (re.compile(rf"^/api/p/{SEG}/state$"), "state"),
    (re.compile(r"^/static/(page\.css|page\.js)$"), "static"),
    (re.compile(r"^/favicon\.ico$"), "favicon"),
]
ROUTE_PUT = re.compile(rf"^/api/p/{SEG}/items/{SEG}$")
STATIC_TYPES = {"page.css": "text/css; charset=utf-8", "page.js": "text/javascript; charset=utf-8"}


class HttpError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code, self.msg = code, msg


class Handler(BaseHTTPRequestHandler):
    server_version = "review-desk"
    sys_version = ""
    timeout = 15  # per-connection socket timeout: a stalled client can't hold a thread forever

    # ---- plumbing -------------------------------------------------------------------------------
    def _send(self, code, body, ctype):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json")

    def _err(self, code, msg):
        if self.path.startswith("/api/"):
            self._json(code, {"error": msg})
        else:
            self._send(code, render.error_page(f"{code}", msg), "text/html; charset=utf-8")

    def log_message(self, *a):
        pass

    @property
    def port(self):
        return self.server.server_address[1]

    def _allowed_hosts(self):
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def _check_host(self):
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or hosts[0].strip() not in self._allowed_hosts():
            raise HttpError(403, "bad Host header")

    def _dispatch(self, fn):
        try:
            self._check_host()
            fn()
        except HttpError as e:
            self._err(e.code, e.msg)
        except Exception as e:  # last resort: never leak a traceback
            self._err(500, f"internal error: {type(e).__name__}")

    def _packet(self, pid):
        entry = self.server.registry.get(pid)
        if entry is None:
            raise HttpError(404, "unknown packet")
        try:
            return load(entry.path)
        except ManifestError as e:
            raise HttpError(500, f"manifest error: {e}")

    # ---- methods --------------------------------------------------------------------------------
    def do_GET(self):
        self._dispatch(self._get)

    def do_HEAD(self):
        self._dispatch(self._get)

    def do_PUT(self):
        self._dispatch(self._put)

    def _not_allowed(self):
        def refuse():
            raise HttpError(405, "method not allowed")
        self._dispatch(refuse)

    do_POST = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def _get(self):
        path = urlsplit(self.path).path
        for rx, name in ROUTES_GET:
            m = rx.match(path)
            if m:
                return getattr(self, f"_get_{name}")(*m.groups())
        raise HttpError(404, "not found")

    def _get_index(self):
        rows = [summarize_entry(e) for e in self.server.registry.entries()]
        self._send(200, render.index_page(rows), "text/html; charset=utf-8")

    def _get_packet(self, pid):
        p = self._packet(pid)
        self._send(200, render.packet_page(pid, p), "text/html; charset=utf-8")

    def _get_raw(self, pid, item_id):
        p = self._packet(pid)
        item = p.item(item_id)
        fp = p.file_path(item) if item else None
        if fp is None or not fp.is_file():
            raise HttpError(404, "no such item file")
        self._send(200, fp.read_bytes(), "text/plain; charset=utf-8")

    def _get_health(self):
        self._json(200, {"app": "review-desk", "port": self.port})

    def _get_state(self, pid):
        p = self._packet(pid)
        try:
            self._json(200, dec.read(p.dir))
        except dec.DecisionsError as e:
            raise HttpError(500, str(e))

    def _get_favicon(self):
        self._send(204, b"", "image/x-icon")

    def _get_static(self, name):
        data = resources.files("review_desk").joinpath("static", name).read_bytes()
        self._send(200, data, STATIC_TYPES[name])

    def _put(self):
        m = ROUTE_PUT.match(urlsplit(self.path).path)
        if not m:
            raise HttpError(404, "not found")
        pid, item_id = m.groups()
        if self.headers.get("X-Review-Desk") != "1":
            raise HttpError(403, "missing X-Review-Desk: 1 header")
        origin = self.headers.get("Origin")
        if origin is not None and origin not in {f"http://{h}" for h in self._allowed_hosts()}:
            raise HttpError(403, "cross-origin write refused")
        if self.headers.get("Sec-Fetch-Site") not in (None, "same-origin", "none"):
            raise HttpError(403, "cross-site write refused")
        if not (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() == "application/json":
            raise HttpError(415, "Content-Type must be application/json")
        body = self._read_body()
        try:
            patch = json.loads(body)
        except ValueError:
            raise HttpError(400, "body is not JSON")
        p = self._packet(pid)
        if p.item(item_id) is None:
            raise HttpError(404, "unknown item")
        try:
            dec.validate_patch(patch)
        except dec.DecisionsError as e:
            raise HttpError(400, str(e))
        try:
            rec = dec.merge_item(p.dir, item_id, patch)
        except dec.DecisionsError as e:  # unreadable decisions file: refuse rather than overwrite
            raise HttpError(500, str(e))
        self._json(200, rec)

    def _read_body(self) -> bytes:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise HttpError(411, "Content-Length required")
        try:
            n = int(raw)
            if n < 0:
                raise ValueError
        except ValueError:
            raise HttpError(400, "bad Content-Length")
        if n > MAX_BODY:
            self.close_connection = True
            self._drain(n)
            raise HttpError(413, f"body over {MAX_BODY} bytes")
        return self.rfile.read(n)

    def _drain(self, n):
        """Discard an oversize body in small chunks (bounded memory) so the client can read the 413."""
        left = min(n, DRAIN_LIMIT)
        try:
            while left > 0:
                chunk = self.rfile.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass


class DeskServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int, registry: Registry):
        self.registry = registry
        super().__init__(("127.0.0.1", port), Handler)


def make_server(port: int, registry: Registry) -> DeskServer:
    return DeskServer(port, registry)


__all__ = ["make_server", "DeskServer", "MAX_BODY", "RegistryError"]
