"""review-desk CLI. Every command is non-interactive so agents can drive it; exit 0 on success, 1 on error."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from .decisions import DecisionsError
from .manifest import MANIFEST, SCAFFOLD, ManifestError
from .registry import Registry, RegistryError, state_dir
from .summary import summarize_dir, summarize_entry, text_report

DEFAULT_PORT = 8791
PLATFORM = sys.platform  # module attribute so tests can pick a platform


def default_port() -> int:
    return int(os.environ.get("REVIEW_DESK_PORT") or DEFAULT_PORT)


def share_requested(flag: bool = False) -> bool:
    return flag or os.environ.get("REVIEW_DESK_SHARE") == "1"


def share_mdns_requested(flag: bool = False) -> bool:
    return flag or os.environ.get("REVIEW_DESK_SHARE_MDNS") == "1"


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


class CliError(Exception):
    pass


# ---- desk process ---------------------------------------------------------------------------------

def probe(port: int) -> str:
    """'desk' if a review-desk answers on the port, 'other' if something else does, 'none' if nothing listens."""
    req = urllib.request.Request(base_url(port) + "api/health", headers={"Host": f"127.0.0.1:{port}"})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=2) as r:
            return "desk" if json.loads(r.read()).get("app") == "review-desk" else "other"
    except urllib.error.HTTPError:
        return "other"
    except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError):
        return "none"


def ensure_running(port: int, reload: bool = False, share: bool = False, mdns: bool = False) -> None:
    """Start `review-desk serve` detached if nothing answers on the port; log to $REVIEW_DESK_STATE/desk-<port>.log.
    With reload (or REVIEW_DESK_RELOAD=1) the detached desk restarts itself when its Python source changes; with share
    (or REVIEW_DESK_SHARE=1) it runs in share mode, and with mdns (or REVIEW_DESK_SHARE_MDNS=1, which needs share too) it
    also accepts this machine's `.local` name."""
    state = probe(port)
    if state == "desk":
        if reload:
            print(f"review-desk: a desk is already running on port {port}; --reload only applies to a desk this command "
                  "starts, so stop that one first", file=sys.stderr)
        if share:
            print(f"review-desk: a desk is already running on port {port}; --share only applies to a desk this command "
                  "starts, so stop that one first", file=sys.stderr)
        if mdns:
            print(f"review-desk: a desk is already running on port {port}; --share-mdns only applies to a desk this command "
                  "starts, so stop that one first", file=sys.stderr)
        return
    if state == "other":
        raise CliError(f"port {port} is in use by something that isn't review-desk; pick another with --port")
    sd = state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    log = open(sd / f"desk-{port}.log", "ab")
    cmd = [sys.executable, "-P", "-m", "review_desk", "serve", "--port", str(port)]  # -P: never import from the current directory
    if reload or os.environ.get("REVIEW_DESK_RELOAD") == "1":
        cmd.append("--reload")
    if share_requested(share):
        cmd.append("--share")
        if share_mdns_requested(mdns):
            cmd.append("--share-mdns")
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    for _ in range(50):
        time.sleep(0.1)
        if probe(port) == "desk":
            print(f"started the desk on {base_url(port)} (log: {sd / f'desk-{port}.log'})", file=sys.stderr)
            return
    raise CliError(f"desk didn't come up on port {port}; see {sd / f'desk-{port}.log'}")


def open_browser(url: str, browser: str | None) -> None:
    """Show `url` in a browser. A named browser opens a new window of that app on macOS
    (`open -na NAME --args --new-window URL`) and is looked up with `webbrowser.get(NAME)` elsewhere.
    Unnamed: `open URL` on macOS (the default browser), `webbrowser` elsewhere."""
    if PLATFORM == "darwin":
        cmd = ["open", "-na", browser, "--args", "--new-window", url] if browser else ["open", url]
        subprocess.run(cmd, check=True)
        return
    try:
        ok = (webbrowser.get(browser) if browser else webbrowser).open(url, new=1)
    except webbrowser.Error as e:
        raise CliError(f"can't start browser {browser!r}: {e}") from None
    if not ok:
        print(f"review-desk: couldn't open a browser; open {url} yourself", file=sys.stderr)


# ---- commands -------------------------------------------------------------------------------------

def cmd_add(a, reg: Registry):
    e = reg.add(a.dir)
    print(f"registered {e.id}  {e.path}")
    print(f"open: review-desk open {e.path}   (page: {base_url(a.port)}p/{e.id}/)")


def cmd_remove(a, reg: Registry):
    if not reg.remove(a.dir):
        raise CliError(f"not registered: {Path(a.dir).expanduser().resolve()}")
    print(f"unregistered {Path(a.dir).expanduser().resolve()} (files untouched)")


def cmd_list(a, reg: Registry):
    rows = [summarize_entry(e) for e in reg.entries()]
    if a.json:
        print(json.dumps({"packets": rows}, indent=1))
        return
    if not rows:
        print("no packets registered")
    for r in rows:
        if "error" in r:
            print(f"{r['id']}  ERROR {r['error']}\n    {r['path']}")
            continue
        c = r["counts"]
        print(f"{r['id']}  {r['title']}\n    {c['total']} items: {c['approve']} approve, {c['revise']} revise, "
              f"{c['drop']} drop, {c['pending']} pending; last touched {r['lastTouched'] or 'never'}\n    {r['path']}")


def cmd_status(a, reg: Registry):
    if a.dir is None:
        rows = [summarize_entry(e) for e in reg.entries()]
        waiting = sum(1 for r in rows if "error" not in r and r["counts"]["pending"])
        if a.json:
            print(json.dumps({"packets": rows, "packetsWithPending": waiting}, indent=1))
        else:
            for r in rows:
                if "error" in r:
                    print(f"ERROR    {r['id']}  {r['error']}")
                else:
                    print(f"{r['counts']['pending']:>3} pending  {r['id']}  {r['title']}")
            print(f"{waiting} packet(s) with pending items")
        return
    s = summarize_dir(a.dir)
    e = reg.find(a.dir)
    s["id"] = e.id if e else None
    s["url"] = f"{base_url(a.port)}p/{e.id}/" if e else None
    print(json.dumps(s, indent=1) if a.json else text_report(s))


def _wait_snapshot(directory) -> str:
    return json.dumps([(i["id"], i["decision"], i["note"]) for i in summarize_dir(directory)["items"]], sort_keys=True)


def cmd_wait(a, reg: Registry):
    """Read-only: poll the packet's decisions until a decision or note changes (or an item is added or removed)."""
    for name in ("start", "max"):
        if getattr(a, name) < 1:
            raise CliError(f"--{name} must be whole seconds >= 1")
    base = _wait_snapshot(a.dir)  # a bad packet fails here, at once
    delay, fails = a.start, 0
    while True:
        time.sleep(delay)
        try:
            cur = _wait_snapshot(a.dir)
            fails = 0
        except (ManifestError, DecisionsError, OSError) as e:  # e.g. review.toml mid-edit; give it a few polls
            fails += 1
            if fails >= 3:
                raise CliError(f"gave up after 3 failed polls: {e}") from None
            cur = base
        if cur != base:
            print("CHANGED")
            print(cur)
            return
        delay = min(delay * 2, a.max)


def cmd_new(a, reg: Registry):
    d = Path(a.dir).expanduser().resolve()
    mf = d / MANIFEST
    if mf.exists():
        raise CliError(f"{mf} already exists; not overwriting")
    d.mkdir(parents=True, exist_ok=True)
    mf.write_text(SCAFFOLD.format(title=json.dumps(a.title or d.name)))
    ex = d / "example-draft.md"
    if not ex.exists():
        ex.write_text("# Example draft\n\nReplace this file, or point the item's `file` at your draft.\n")
    print(f"wrote {mf}\nedit it, then: review-desk add {d}")


def cmd_serve(a, reg: Registry):
    from .server import make_server
    share = share_requested(a.share)
    if a.share_mdns and not share:
        raise CliError("--share-mdns needs --share")
    mdns = share and share_mdns_requested(a.share_mdns)  # the env switch alone, without sharing, is ignored
    if a.background:
        ensure_running(a.port, reload=a.reload, share=a.share, mdns=a.share_mdns)
        print(base_url(a.port))
        return
    if probe(a.port) != "none":
        raise CliError(f"port {a.port} is already in use")
    if a.reload:
        from . import reload as rl
        print(f"review-desk serving {base_url(a.port)} and restarting on source changes", flush=True)
        rl.stop_on_sigterm()
        return rl.supervise(lambda: rl.spawn_desk(a.port, share, mdns), rl.snapshot)
    srv = make_server(a.port, reg, share=share, mdns=mdns)
    srv.watcher.start()  # records external changes to items (open packets about every second, others every 10 minutes)
    print(f"review-desk serving {base_url(a.port)} (registry: {reg.file})", flush=True)
    if share:
        print("share mode: the local network can reach this desk only while a 4-hour share window is open "
              "(Share button on the page)" + ("; its .local name is accepted too" if mdns else ""), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


def cmd_open(a, reg: Registry):
    url = base_url(a.port)
    if a.dir:
        e = reg.find(a.dir)
        if e is None:
            e = reg.add(a.dir)
            print(f"registered {e.id}  {e.path}", file=sys.stderr)
        url += f"p/{e.id}/"
    if not a.no_browser:
        ensure_running(a.port)
        open_browser(url, a.browser or os.environ.get("REVIEW_DESK_BROWSER") or None)
    print(url)


def cmd_share(a, reg: Registry):
    """Read or change the desk's share window through its localhost API (the same route the page's button uses)."""
    if probe(a.port) != "desk":
        raise CliError(f"no desk on port {a.port}; start one with `review-desk serve --share`")
    headers = {"Host": f"127.0.0.1:{a.port}"}
    data = None
    if a.action != "status":
        data = json.dumps({"action": a.action}).encode()
        headers.update({"Content-Type": "application/json", "X-Review-Desk": "1"})
    req = urllib.request.Request(base_url(a.port) + "api/share", data=data, headers=headers)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as r:
            s = json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except ValueError:
            msg = None
        raise CliError(msg or f"the desk answered {e.code}") from None
    except (urllib.error.URLError, ConnectionError, TimeoutError, ValueError) as e:
        raise CliError(f"couldn't reach the desk: {e}") from None
    if a.json:
        print(json.dumps(s, indent=1))
    elif not s["enabled"]:
        print("share mode is off (start the desk with `review-desk serve --share`)")
    elif not s["active"]:
        print("sharing: off")
    else:
        sec = s["remaining"]
        print(f"sharing: on, {sec // 3600}:{sec // 60 % 60:02}:{sec % 60:02} left")
        for u in s["urls"]:
            print(f"  {u['url']}  ({u['label']})")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="review-desk", description="Local approval desk for agent drafts.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def port_arg(sp):
        sp.add_argument("--port", type=int, default=default_port(), help=f"desk port (default {DEFAULT_PORT}, or $REVIEW_DESK_PORT)")

    s = sub.add_parser("add", help="register a packet directory (must hold a valid review.toml)")
    s.add_argument("dir"); port_arg(s); s.set_defaults(fn=cmd_add)
    s = sub.add_parser("remove", help="unregister a packet (its files are untouched)")
    s.add_argument("dir"); s.set_defaults(fn=cmd_remove)
    s = sub.add_parser("list", help="registered packets with decision counts")
    s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_list)
    s = sub.add_parser("status", help="decisions and notes for one packet (or a summary of all registered ones)")
    s.add_argument("dir", nargs="?"); s.add_argument("--json", action="store_true"); port_arg(s); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("wait", help="block until a packet's decisions or notes change, then print CHANGED and a snapshot "
                                    "(read-only; run it in the background to be re-invoked when the reviewer decides)")
    s.add_argument("dir")
    s.add_argument("--start", type=int, default=int(os.environ.get("POLL_START") or 15), help="first poll delay, seconds (15)")
    s.add_argument("--max", type=int, default=int(os.environ.get("POLL_MAX") or 600), help="longest poll delay, seconds (600)")
    s.set_defaults(fn=cmd_wait)
    s = sub.add_parser("new", help="scaffold a review.toml in a directory")
    s.add_argument("dir"); s.add_argument("--title"); s.set_defaults(fn=cmd_new)
    s = sub.add_parser("serve", help="run the desk (127.0.0.1 only, unless --share); foreground unless --background")
    port_arg(s)
    s.add_argument("--background", action="store_true", help="start detached if not already running, print the URL, exit")
    s.add_argument("--reload", action="store_true",
                   help="restart the desk when its Python source changes (page.js/page.css already hot-swap without this); "
                        "REVIEW_DESK_RELOAD=1 does the same for desks that `open` starts. Edits to cli.py and reload.py "
                        "restart only the desk, not the supervisor")
    s.add_argument("--share", action="store_true",
                   help="allow sharing with the local network: binds all interfaces, but refuses every other machine "
                        "until a 4-hour share window is switched on from the page (or POST /api/share on localhost); "
                        "REVIEW_DESK_SHARE=1 does the same for desks that `open` starts")
    s.add_argument("--share-mdns", action="store_true",
                   help="with --share: also accept (and list in the Share tooltip) this machine's <name>.local mDNS name. "
                        "Off by default, since anyone on the network can claim a .local name; REVIEW_DESK_SHARE_MDNS=1 "
                        "does the same for desks that `open` starts (when they run with --share)")
    s.set_defaults(fn=cmd_serve)
    s = sub.add_parser("open", help="open the desk or a packet in a browser; starts the desk if needed; "
                                    "registers the packet if it isn't yet")
    s.add_argument("dir", nargs="?"); port_arg(s)
    s.add_argument("--browser", metavar="NAME", help="browser to use (default $REVIEW_DESK_BROWSER, else the system "
                                                     "default); on macOS an app name, opened in a new window")
    s.add_argument("--no-browser", action="store_true", help="only print the URL (don't start the desk or a browser)")
    s.set_defaults(fn=cmd_open)
    s = sub.add_parser("share", help="show or change sharing with the local network (the desk must run with --share): "
                                     "`on` opens a 4-hour window, or resets a running one to a full 4 hours; `off` closes it")
    s.add_argument("action", nargs="?", choices=["status", "on", "off"], default="status")
    s.add_argument("--json", action="store_true"); port_arg(s); s.set_defaults(fn=cmd_share)
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    try:
        a.fn(a, Registry.default())
        return 0
    except (CliError, RegistryError, ManifestError, DecisionsError, subprocess.CalledProcessError) as e:
        print(f"review-desk: {e}", file=sys.stderr)
        return 1
