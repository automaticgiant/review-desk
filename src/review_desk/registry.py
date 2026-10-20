"""Registry of packets the desk may serve, by absolute path: $REVIEW_DESK_STATE/packets.json
(default ~/.local/state/review-desk). Only registered packets are reachable over HTTP."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .fsutil import atomic_write_json
from .manifest import ManifestError, load, slugify

_lock = threading.Lock()


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    id: str
    path: str
    added: str


def state_dir() -> Path:
    return Path(os.environ.get("REVIEW_DESK_STATE") or "~/.local/state/review-desk").expanduser()


def packet_id(path: Path) -> str:
    """Readable and stable: the last two path parts slugged, plus 6 hex of the path's sha1 so equal names differ."""
    base = slugify("-".join(path.parts[-2:]))[:40].strip("-") or "packet"
    return f"{base}-{hashlib.sha1(str(path).encode()).hexdigest()[:6]}"


class Registry:
    def __init__(self, file: Path):
        self.file = Path(file)

    @classmethod
    def default(cls) -> "Registry":
        return cls(state_dir() / "packets.json")

    def _read(self) -> list[Entry]:
        if not self.file.exists():
            return []
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return [Entry(**e) for e in data["packets"]]
        except (ValueError, KeyError, TypeError) as e:
            raise RegistryError(f"{self.file} is unreadable ({e}); fix or delete it by hand") from None

    def _write(self, entries: list[Entry]) -> None:
        self.file.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.file, {"packets": [asdict(e) for e in entries]})

    def entries(self) -> list[Entry]:
        return self._read()

    def get(self, pid: str) -> Entry | None:
        return next((e for e in self._read() if e.id == pid), None)

    def find(self, directory) -> Entry | None:
        p = str(Path(directory).expanduser().resolve())
        return next((e for e in self._read() if e.path == p), None)

    def add(self, directory) -> Entry:
        path = Path(directory).expanduser().resolve()
        try:
            load(path)
        except ManifestError as e:
            raise RegistryError(str(e)) from None
        with _lock:
            entries = self._read()
            for e in entries:
                if e.path == str(path):
                    return e
            e = Entry(id=packet_id(path), path=str(path),
                      added=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            self._write(entries + [e])
            return e

    def remove(self, directory) -> bool:
        p = str(Path(directory).expanduser().resolve())
        with _lock:
            entries = self._read()
            keep = [e for e in entries if e.path != p]
            if len(keep) == len(entries):
                return False
            self._write(keep)
            return True
