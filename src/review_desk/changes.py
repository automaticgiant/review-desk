"""External changes to items, kept in review-changes.json beside the manifest.

  {"seen": {"<kind>:<item id>": "<fingerprint>"}, "items": {item_id: [{"at": iso, "kind": ..., "detail"?: ..., "coarse"?: true}, ...]}}

"External" means not made by the reviewer's page. Each entry has a kind:
  draft          the item's draft file changed on disk (by content: a touch or a same-text save doesn't count)
  manifest       the item's entry in review.toml changed (title, kind, recommendation, file, links, facts)
  decision       the decision changed through the API without the page (what an agent does)
  note           the note changed through the API without the page
  decisions-file review-decisions.json was edited on disk for this item
An entry marked "coarse" came from a scan that had not looked for a while, so it may stand for several changes.

Two sources feed it. `Watcher` scans registered packets by content hash (not file events: a hash never misses a net
change, and a renamed-over file needs no re-arming). A packet is scanned about once a second while a page has it open (the
page's poll of /api/p/<pid>/changes calls `touch`), and every CLOSED_INTERVAL seconds otherwise. A scan that follows a gap
longer than COARSE_AFTER seconds marks what it finds coarse, and stamps it with the file's own modified time rather than
the later time the scan noticed. The PUT handler calls `record_write` after a decision write; the page marks its own
writes with X-Review-Page, so only the others are listed. Both take LOCK, so a write the scan could see half done is never
reported as a file edit. Files are read through fsutil.read_regular, which opens non-blocking and checks the open
descriptor, so a FIFO or device swapped in after a check on the path can't hang a scan (or the lock).

A fingerprint with no stored value is a baseline, not a change: the first scan of a packet records nothing. A file modified within the last SETTLE_SECONDS is left for the next
pass, so a save that truncates and then writes is one change, not two. Entries are
oldest first, at most MAX_ENTRIES per item. The file is derived data: an unreadable one is started afresh. It is also
only as trustworthy as the packet directory: whoever can write there can rewrite or forge it.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import decisions as dec
from .fsutil import NotRegularError, atomic_write_json, read_regular
from .manifest import ManifestError, load

CHANGES = "review-changes.json"
MAX_ENTRIES = 50
MAX_HASHED = 8 * 1024 * 1024  # a larger file is fingerprinted by size and mtime only
MAX_LOG_BYTES = 2 * 1024 * 1024  # a larger review-changes.json is treated as unreadable
SWEEP_SECONDS = 30  # a file whose stat is unchanged is rehashed this often anyway (a same-size edit with a kept mtime)
OPEN_INTERVAL = 1.0  # scan period for a packet a page has open
CLOSED_INTERVAL = 600.0  # and for every other packet
OPEN_TTL = 15.0  # a packet counts as open this long after the page's last poll (the page polls every 3 s while visible)
COARSE_AFTER = 5.0  # a scan this long after the previous one can't say how many changes it found
SETTLE_SECONDS = 0.5  # a file modified this recently may be mid-write (truncated, half written): look again next pass
KINDS = ("draft", "manifest", "decision", "note", "decisions-file")
LOCK = threading.Lock()


def path_for(packet_dir) -> Path:
    return Path(packet_dir) / CHANGES


def now_iso() -> str:
    return iso_from_epoch(time.time())


def iso_from_epoch(sec: float) -> str:
    return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _regular(path):
    """os.stat of a regular file, else None. Never opens it, so a FIFO or device can't block us."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st if stat.S_ISREG(st.st_mode) else None


def read(packet_dir) -> dict:
    """The item -> entries map for the page; {} when there is none or the file is unreadable."""
    return _read_file(packet_dir)["items"]


def _read_file(packet_dir) -> dict:
    try:
        data = json.loads(read_regular(path_for(packet_dir), MAX_LOG_BYTES).decode("utf-8"))
    except (OSError, ValueError, RecursionError):  # missing, a FIFO, too big, not JSON, or nested to the point of overflow
        data = None
    if not isinstance(data, dict):
        data = {}
    seen, items = data.get("seen"), data.get("items")
    seen = {k: v for k, v in (seen if isinstance(seen, dict) else {}).items() if isinstance(v, str) and ":" in k}
    items = {k: [e for e in v if isinstance(e, dict)] for k, v in (items if isinstance(items, dict) else {}).items()
             if isinstance(v, list)}
    return {"seen": seen, "items": items}


def _add(data: dict, item_id: str, kind: str, at: str, detail: str = "", coarse: bool = False) -> None:
    entry = {"at": at, "kind": kind}
    if detail:
        entry["detail"] = detail
    if coarse:
        entry["coarse"] = True
    entries = data["items"].get(item_id, []) + [entry]
    data["items"][item_id] = entries[-MAX_ENTRIES:]


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def record_fingerprint(rec) -> str:
    """What a decision record is, for change purposes: decision and note, not updatedAt."""
    rec = rec if isinstance(rec, dict) else {}
    return _sha(json.dumps([rec.get("decision") or "", rec.get("note") or ""]).encode())


def item_fingerprint(item) -> str:
    return _sha(json.dumps({"title": item.title, "kind": item.kind, "recommendation": item.recommendation,
                            "file": item.file, "links": [[l.label, l.url] for l in item.links],
                            "facts": item.facts}, sort_keys=True).encode())


def _label(decision) -> str:
    return decision or "not reviewed"


def record_write(packet_dir, item_id: str, before, after, external: bool, at: str | None = None) -> None:
    """After a decision write (call with LOCK held): remember the item's new state so the scan doesn't call it a file
    edit, and when the write didn't come from the page, list what it changed."""
    before = before if isinstance(before, dict) else {}
    data = _read_file(packet_dir)
    at = at or now_iso()
    key = f"dec:{item_id}"
    prior = data["seen"].get(key)
    if prior is not None and prior != record_fingerprint(before):
        _add(data, item_id, "decisions-file", at, coarse=True)  # edited on disk since the last scan: don't let this write hide it
    data["seen"][key] = record_fingerprint(after)
    if external:
        if (before.get("decision") or "") != (after.get("decision") or ""):
            _add(data, item_id, "decision", at, f"{_label(before.get('decision'))} → {_label(after.get('decision'))}")
        if (before.get("note") or "") != (after.get("note") or ""):
            _add(data, item_id, "note", at, "note cleared" if not (after.get("note") or "").strip() else "note changed")
    atomic_write_json(path_for(packet_dir), data)


class Watcher:
    """Decides which packets to scan and when. `start()` runs it in a daemon thread; `touch(entry)` is the page's poll."""

    def __init__(self, registry, clock=time.monotonic, wall=now_iso, open_interval: float = OPEN_INTERVAL,
                 closed_interval: float = CLOSED_INTERVAL, settle: float = SETTLE_SECONDS, epoch=time.time):
        self.registry, self._clock, self._wall = registry, clock, wall
        self.settle, self._epoch = settle, epoch  # epoch: seconds since 1970, comparable with file mtimes
        self.open_interval, self.closed_interval = open_interval, closed_interval
        self._files: dict[str, tuple] = {}  # path -> (mtime_ns, size, fingerprint, hashed_at)
        self._packets: dict[str, tuple] = {}  # packet dir -> (manifest signature, loaded_at, Packet, item fingerprints, draft paths, mtime_ns)
        self._last: dict[str, tuple] = {}  # packet dir -> (clock, wall) of its last scan
        self._open: dict[str, float] = {}  # packet dir -> clock of the page's last poll
        self._errors: dict[str, str] = {}
        self._scanning = threading.Lock()  # one scan at a time: a page's first poll and the thread's tick must not overlap
        self._stop = threading.Event()

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run, name="review-desk-changes", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self.scan_all()
        while not self._stop.wait(self.open_interval):
            self.tick()

    def scan_all(self) -> None:
        for entry in self.registry.entries():
            self.scan_packet(entry)

    def tick(self) -> None:
        """Scan the packets that are due: open ones every open_interval, the rest every closed_interval."""
        now = self._clock()
        live = set()
        for entry in self.registry.entries():
            key = str(entry.path)
            live.add(key)
            last = self._last.get(key)
            every = self.open_interval if self._is_open(key, now) else self.closed_interval
            if last is None or now - last[0] >= every:
                self.scan_packet(entry)
        for gone in set(self._packets) - live:
            root = str(self._packets[gone][2].dir) + os.sep
            self._files = {k: v for k, v in self._files.items() if not k.startswith(root)}
        for table in (self._packets, self._last, self._open, self._errors):
            for gone in set(table) - live:
                del table[gone]

    def _is_open(self, key: str, now: float) -> bool:
        seen = self._open.get(key)
        return seen is not None and now - seen <= OPEN_TTL

    def touch(self, entry) -> None:
        """A page is polling this packet. The first poll after it was closed scans at once, so the page starts current."""
        key, now = str(entry.path), self._clock()
        was_open = self._is_open(key, now)
        self._open[key] = now
        if not was_open:
            self.scan_packet(entry)

    def scan_packet(self, entry) -> None:
        key = str(entry.path)
        try:
            with self._scanning:
                self._scan(entry)
            self._errors.pop(key, None)
        except Exception as e:  # one broken packet (bad manifest, vanished directory) must not stop the others
            msg = f"{type(e).__name__}: {e}"
            if self._errors.get(key) != msg:  # say it once per distinct problem, in the desk's log
                self._errors[key] = msg
                print(f"review-desk: change scan of {key} failed ({msg})", file=sys.stderr, flush=True)

    def _fingerprint(self, path: Path):
        """(sha1 of the file, its mtime_ns); ("missing", None) or ("special", None) when it isn't a regular file.
        Rehashed only when its stat changed, or every SWEEP_SECONDS."""
        key, now = str(path), self._clock()
        st = _regular(path)
        if st is None:
            self._files.pop(key, None)
            return ("special" if os.path.exists(path) else "missing"), None
        old = self._files.get(key)
        if old and old[:2] == (st.st_mtime_ns, st.st_size) and now - old[3] < SWEEP_SECONDS:
            return old[2], st.st_mtime_ns
        if st.st_size > MAX_HASHED:
            fp = f"stat:{st.st_size}:{st.st_mtime_ns}"
        else:
            try:
                fp = _sha(read_regular(path))
            except NotRegularError:
                return "special", None
            except OSError:
                return "missing", None
        self._files[key] = (st.st_mtime_ns, st.st_size, fp, now)
        return fp, st.st_mtime_ns

    def _packet(self, directory):
        """(packet, {item id: fingerprint}, {item id: draft path}, manifest mtime_ns), parsed and resolved again only when
        review.toml's stat changes or every SWEEP_SECONDS: resolving 20 draft paths on every pass was most of a scan."""
        st = _regular(Path(directory) / "review.toml")
        if st is None:
            raise ManifestError("no manifest")
        sig, now = (st.st_mtime_ns, st.st_size), self._clock()
        cached = self._packets.get(str(directory))
        if cached and cached[0] == sig and now - cached[1] < SWEEP_SECONDS:
            return cached[2:]
        packet = load(directory)
        loaded = (packet, {it.id: item_fingerprint(it) for it in packet.items},
                  {it.id: packet.file_path(it) for it in packet.items}, st.st_mtime_ns)
        self._packets[str(directory)] = (sig, now, *loaded)
        return loaded

    @staticmethod
    def _stamp(mtime_ns, coarse: bool, prev_wall, now_wall: str) -> str:
        """When to say a change happened. A scan that looked recently knows: now. A late one uses the file's own mtime, unless
        that is in the future or no newer than the previous scan (a tool that kept the old mtime), when it can't help."""
        if not coarse or mtime_ns is None:
            return now_wall
        m = iso_from_epoch(mtime_ns / 1e9)
        if m > now_wall or (prev_wall is not None and m <= prev_wall):
            return now_wall
        return m

    def _scan(self, entry) -> None:
        key = str(entry.path)
        now_c, now_w = self._clock(), self._wall()
        prev = self._last.get(key)
        self._last[key] = (now_c, now_w)  # before the work: a packet that fails is retried after its interval, not in a loop
        coarse = prev is None or now_c - prev[0] > COARSE_AFTER
        packet, item_fps, drafts, manifest_mtime = self._packet(entry.path)
        fresh = {}  # key -> (item id, kind, fingerprint, mtime_ns)
        for it in packet.items:
            fresh[f"item:{it.id}"] = (it.id, "manifest", item_fps[it.id], manifest_mtime)
            if drafts[it.id] is not None:
                fp, mtime = self._fingerprint(drafts[it.id])
                fresh[f"draft:{it.id}"] = (it.id, "draft", fp, mtime)
        with LOCK:  # decisions are read under the lock: a write that landed since is already in `seen`
            try:
                state = dec.read(packet.dir)
            except dec.DecisionsError:
                state = None  # unreadable (or not a regular file): say nothing about decisions this pass
            if state is not None:
                st = _regular(dec.path_for(packet.dir))
                for it in packet.items:
                    fresh[f"dec:{it.id}"] = (it.id, "decisions-file", record_fingerprint(state.get(it.id)),
                                             st.st_mtime_ns if st else None)
            data = _read_file(packet.dir)
            dirty = False
            for k, (item_id, kind, fp, mtime) in fresh.items():
                old = data["seen"].get(k)
                if old == fp:
                    continue
                if old is not None and mtime is not None and self._epoch() - mtime / 1e9 < self.settle:
                    continue  # possibly mid-write: don't commit a torn read as a change (an empty file, then the real one)
                dirty = True
                data["seen"][k] = fp
                if old is not None:
                    _add(data, item_id, kind, self._stamp(mtime, coarse, prev[1] if prev else None, now_w), coarse=coarse)
            ids = {it.id for it in packet.items}
            stale = [k for k in data["seen"] if k.partition(":")[2] not in ids]
            for k in stale:
                del data["seen"][k]
            if dirty or stale:
                atomic_write_json(path_for(packet.dir), data)
