"""Regenerate the README screenshots from dummy packets.

Builds demo packets under ~/review-desk-demo (the pages show it as ~/..., so no username appears), starts a
scratch desk on port 8794 (registry in ~/review-desk-demo/state, so your own desk and registry are never touched), sets decisions through the desk's
API like the page does, captures the PNGs into this directory with Playwright's Chromium, then stops the desk and
deletes the demo directory.

Rerun from the repository root (Playwright for Python must be importable; it isn't a project dependency):

    uv run --offline --with playwright python docs/screenshots/make.py
    # or, with playwright on PYTHONPATH:  uv run --offline python docs/screenshots/make.py

Options: --chromium PATH (or $PLAYWRIGHT_CHROMIUM) picks the browser binary; otherwise the newest Chromium in the
Playwright cache is used, else Playwright's default. --keep leaves the demo directory in place.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 8794
DEMO = Path.home() / "review-desk-demo"
MARKER = ".made-by-review-desk-screenshots"  # only a folder this script made is ever deleted
OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[1]
WIDTH = 1200

RELEASE = {
    "title": "Release 2.4 follow-ups",
    "intro": "Follow-ups from the 2.4 release review. Approve what's ready to file or send, mark anything that needs "
             "another pass as **revise** with a note, and drop what we shouldn't do.",
    "links": [("Release 2.4 milestone", "https://example.com/milestones/2.4")],
    "items": [
        dict(id="export-retry", title="Add retry to the export job", kind="new Task",
             recommendation="**File** it: three failed nightly exports last week, all transient.",
             facts={"Overlaps": "None found"}, links=[("Failed run", "https://example.com/runs/1182")],
             body="""# Add retry to the export job

The nightly export fails outright when the storage API returns a transient `503`. Last week that happened three
times, and each time someone reran the job by hand.

## Proposal

Each upload should:
- Retry up to **3 times**, with exponential backoff (2 s, 4 s, 8 s).
- Retry only on transient errors; fail fast on anything else:
  - `429` and `5xx`: retry;
  - any other `4xx`: fail at once.
- Log each retry with the attempt number, so flaky days are visible.

```python
@retry(on=(TooManyRequests, ServerError), attempts=3, backoff=2.0)
def upload(chunk): ...

log.warning("upload retry", extra={"job": job.id, "chunk": chunk.index, "attempt": attempt, "status": err.status, "wait_s": delay})
```

## Acceptance criteria

1. A single `503` no longer fails the job.
2. A permanent error still fails within one attempt.
3. Retries show up in the job log.

See the [failed run](https://example.com/runs/1182) for the original error.
"""),
        dict(id="reply-cache-pr", title="Reply to the reviewer on the cache PR", kind="PR reply",
             recommendation="**Send** after the revision below.",
             links=[("Cache PR", "https://example.com/pulls/431")],
             body="""Thanks for the careful review. On the eviction question: entries expire after 10 minutes, and the
cache is bounded at 5,000 keys, so memory stays flat under load. I've added a test for the expiry path.
"""),
        dict(id="release-notes", title="Draft release notes", kind="document",
             recommendation="**Approve** as drafted.",
             body="""# Release 2.4

## Highlights

- **Faster search.** Results for large projects load about twice as fast.
- **Scheduled exports.** Exports can now run on a schedule; see [the guide](https://example.com/docs/exports).

## Changes

- The `--format` flag now accepts `csv` and `jsonl`.
- Sessions time out after 12 hours of inactivity (was 24).

## Deprecations

The v1 endpoint is deprecated and will be removed in 2.6. Move to v2 before then.
"""),
        dict(id="deprecate-v1", title="Deprecate the v1 endpoint", kind="new Task",
             recommendation="**Drop**: covered by the deprecation notice in the release notes.",
             facts={"Overlaps": "[Release notes item](#release-notes)"},
             body="""Announce the v1 endpoint deprecation, add a `Deprecation` response header, and remove the endpoint
in 2.6.
"""),
        dict(id="flaky-test", title="Quarantine the flaky upload test", kind="new Task",
             recommendation="**File** it, low priority.",
             body="""`test_upload_resume` fails about one run in twenty on CI. Quarantine it and open a follow-up to fix
the race in the fixture.
"""),
    ],
    "decisions": [
        ("export-retry", {"decision": "approve"}),
        ("reply-cache-pr", {"decision": "revise", "note": "Mention the migration flag, and link the new test."}),
        ("deprecate-v1", {"decision": "drop", "note": "Already covered by the release notes."}),
    ],
}

DOCS = {
    "title": "Docs cleanup",
    "intro": "Small fixes from the docs review.",
    "links": [],
    "items": [
        dict(id="fix-install", title="Fix the install steps for Windows", kind="docs edit",
             body="Replace the `export` line with `set` for `cmd.exe`, and add a PowerShell example.\n"),
        dict(id="split-faq", title="Split the FAQ into setup and usage", kind="docs edit",
             body="The FAQ has grown to 40 entries. Split it into two pages.\n"),
        dict(id="broken-links", title="Repair broken links in the API guide", kind="docs edit",
             body="Six links in the API guide point to pages that moved.\n"),
        dict(id="style-guide", title="Adopt the sentence-case heading rule", kind="proposal",
             body="Use sentence case for all headings.\n"),
    ],
    "decisions": [
        ("fix-install", {"decision": "approve"}),
        ("broken-links", {"decision": "approve"}),
        ("split-faq", {"decision": "revise", "note": "Split this into two tickets."}),
    ],
}

ONBOARDING = {
    "title": "Onboarding guide edits",
    "intro": "Suggested edits to the onboarding guide.",
    "links": [],
    "items": [
        dict(id="first-day", title="Rewrite the first-day checklist", kind="docs edit",
             body="Shorten the checklist to the five things that matter on day one.\n"),
        dict(id="glossary", title="Add a glossary", kind="docs edit", body="Define the ten terms new people ask about most.\n"),
    ],
    "decisions": [],
}

PACKETS = {"release-2.4": RELEASE, "docs-cleanup": DOCS, "onboarding": ONBOARDING}


def toml_str(s: str) -> str:
    return json.dumps(s)  # a JSON string is a valid TOML basic string


def write_packet(d: Path, spec: dict) -> None:
    d.mkdir(parents=True)
    lines = [f"title = {toml_str(spec['title'])}", f"intro = {toml_str(spec['intro'])}"]
    if spec["links"]:
        lines.append("links = [" + ", ".join(f"{{label = {toml_str(l)}, url = {toml_str(u)}}}" for l, u in spec["links"]) + "]")
    for it in spec["items"]:
        lines += ["", "[[items]]", f"id = {toml_str(it['id'])}", f"title = {toml_str(it['title'])}",
                  f"kind = {toml_str(it['kind'])}", f"file = {toml_str(it['id'] + '.md')}"]
        if it.get("recommendation"):
            lines.append(f"recommendation = {toml_str(it['recommendation'])}")
        if it.get("links"):
            lines.append("links = [" + ", ".join(f"{{label = {toml_str(l)}, url = {toml_str(u)}}}" for l, u in it["links"]) + "]")
        if it.get("facts"):
            lines.append("[items.facts]")
            lines += [f"{k} = {toml_str(v)}" for k, v in it["facts"].items()]
        (d / f"{it['id']}.md").write_text(it["body"])
    (d / "review.toml").write_text("\n".join(lines) + "\n")


def cli(env, *args) -> str:
    return subprocess.run([sys.executable, "-m", "review_desk", *args], env=env, check=True,
                          capture_output=True, text=True, cwd=ROOT).stdout


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for(cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def put(pid: str, item: str, patch: dict) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/api/p/{pid}/items/{item}", data=json.dumps(patch).encode(), method="PUT",
        headers={"Host": f"127.0.0.1:{PORT}", "Content-Type": "application/json", "X-Review-Desk": "1"})
    with urllib.request.urlopen(req) as r:
        r.read()


def find_chromium(arg: str | None) -> str | None:
    if arg or os.environ.get("PLAYWRIGHT_CHROMIUM"):
        return arg or os.environ["PLAYWRIGHT_CHROMIUM"]
    caches = [Path.home() / "Library/Caches/ms-playwright", Path.home() / ".cache/ms-playwright"]
    pats = ["chromium-*/chrome-mac*/*.app/Contents/MacOS/*", "chromium-*/chrome-linux*/chrome"]
    hits = sorted(h for c in caches for p in pats for h in glob.glob(str(c / p)))
    return hits[-1] if hits else None


def capture(chromium: str | None, pids: dict) -> None:
    from playwright.sync_api import sync_playwright

    base = f"http://127.0.0.1:{PORT}"
    rel = f"{base}/p/{pids['release-2.4']}/"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=chromium)

        def page(scheme="light", height=900):
            ctx = browser.new_context(viewport={"width": WIDTH, "height": height}, color_scheme=scheme,
                                      locale="en-US", timezone_id="UTC")
            return ctx.new_page()

        def packet(p):
            p.goto(rel)
            p.wait_for_function("document.getElementById('conn').textContent.startsWith('Connected')")
            p.evaluate("document.activeElement && document.activeElement.blur()")

        def top_of(p, upto: str, path: str, margin=12):
            # Clip from the top of the page to the bottom of the element matched by `upto`, plus a margin.
            box = p.locator(upto).bounding_box()
            p.screenshot(path=path, full_page=True,
                         clip={"x": 0, "y": 0, "width": WIDTH, "height": box["y"] + box["height"] + margin})

        # 1. index
        p = page(height=700)
        p.goto(base + "/")
        p.wait_for_selector(".packets")
        top_of(p, ".packets", str(OUT / "index.png"), margin=32)

        # 2-3. packet page, light and dark: the bar, the intro and the first four cards
        for scheme in ("light", "dark"):
            p = page(scheme)
            packet(p)
            top_of(p, "#deprecate-v1", str(OUT / f"packet-{scheme}.png"))

        # 4. one card with its draft open
        p = page()
        packet(p)
        p.evaluate("document.querySelector('#export-retry details').open = true")
        p.check("#wrap-toggle")  # the long log line wraps instead of scrolling
        p.evaluate("document.activeElement && document.activeElement.blur()")
        # A clip of the full page, not an element shot: that scrolls, and the sticky bar would cover the card.
        box = p.locator("#export-retry").bounding_box()
        pad = 16
        p.screenshot(path=str(OUT / "draft-expanded.png"), full_page=True,
                     clip={"x": box["x"] - pad, "y": box["y"] - pad,
                           "width": box["width"] + 2 * pad, "height": box["height"] + 2 * pad})

        # 5. the offline banner: stop the desk with the page open and wait for the poll to notice
        p = page(height=700)
        packet(p)
        stop_desk()
        p.wait_for_selector("#offline:not([hidden])", timeout=15000)
        p.wait_for_function("document.querySelector('.decide button').disabled")
        p.screenshot(path=str(OUT / "offline.png"))
        browser.close()


DESK: subprocess.Popen | None = None


def stop_desk() -> None:
    global DESK
    if DESK and DESK.poll() is None:
        DESK.terminate()
        DESK.wait(10)
    DESK = None
    wait_for(lambda: not port_in_use(PORT))


def main() -> int:
    global DESK
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chromium", help="Chromium binary (default $PLAYWRIGHT_CHROMIUM, else the Playwright cache)")
    ap.add_argument("--keep", action="store_true", help="keep ~/review-desk-demo afterwards")
    a = ap.parse_args()
    if port_in_use(PORT):
        sys.exit(f"port {PORT} is busy; stop whatever is on it first")

    if DEMO.exists() and not (DEMO / MARKER).exists():
        sys.exit(f"{DEMO} exists and wasn't made by this script; move it or delete it first")
    shutil.rmtree(DEMO, ignore_errors=True)
    DEMO.mkdir()
    (DEMO / MARKER).touch()
    env = {**os.environ, "REVIEW_DESK_STATE": str(DEMO / "state"), "REVIEW_DESK_PORT": str(PORT)}
    pids = {}
    try:
        for name, spec in PACKETS.items():
            write_packet(DEMO / name, spec)
            pids[name] = cli(env, "add", str(DEMO / name)).split()[1]
        DESK = subprocess.Popen([sys.executable, "-m", "review_desk", "serve", "--port", str(PORT)], env=env, cwd=ROOT,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not wait_for(lambda: port_in_use(PORT)):
            sys.exit("scratch desk didn't start")
        for name, spec in PACKETS.items():
            for item, patch in spec["decisions"]:
                put(pids[name], item, patch)
        capture(find_chromium(a.chromium), pids)
    finally:
        stop_desk()
        if not a.keep and (DEMO / MARKER).exists():
            shutil.rmtree(DEMO, ignore_errors=True)
    for f in sorted(OUT.glob("*.png")):
        print(f"{f.name}  {f.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
