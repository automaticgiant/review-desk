"""`serve --reload`: restart the desk when its own Python source changes.

The page script and stylesheet already hot-swap in open pages (see server.py), so only `.py` files are watched. A small
supervisor runs a plain `serve` as a child process and replaces it when a source file's mtime changes; it needs no
dependency and watches nothing outside the package. A desk that crashes (say, a syntax error mid-edit) is not respawned
in a loop: the supervisor says so once and waits for the next change.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import time
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent


def snapshot(root: Path = PACKAGE) -> dict:
    """{path: mtime_ns} for every .py file under root. A file that vanishes mid-scan (an editor's atomic save) is skipped."""
    out = {}
    for p in Path(root).rglob("*.py"):
        try:
            out[str(p)] = p.stat().st_mtime_ns
        except FileNotFoundError:
            continue
    return out


def _say(msg: str) -> None:
    print(msg, flush=True)  # flushed: the detached desk's log is a file, and stdout is block-buffered there


def stop_on_sigterm() -> None:
    """Make SIGTERM leave supervise() the way Ctrl-C does, so the desk it runs is stopped rather than orphaned."""
    def handler(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, handler)


def spawn_desk(port: int):
    return subprocess.Popen([sys.executable, "-m", "review_desk", "serve", "--port", str(port)])


def _stop(child) -> None:
    if child.poll() is None:
        child.terminate()
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def supervise(spawn, snap, sleep=time.sleep, interval: float = 0.5, settle: float = 0.3, log=_say) -> int:
    child = spawn()
    seen, crashed = snap(), False
    try:
        while True:
            sleep(interval)
            now = snap()
            if now != seen:
                sleep(settle)  # a save that touches several files lands as one restart
                seen = snap()
                log("source changed: restarting the desk")
                _stop(child)
                child, crashed = spawn(), False
            elif child.poll() is not None and not crashed:
                crashed = True
                log(f"the desk exited (code {child.poll()}); waiting for a change")
    except KeyboardInterrupt:
        pass
    finally:
        _stop(child)
    return 0
