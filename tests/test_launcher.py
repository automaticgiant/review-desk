"""bin/review-desk must work when installed as a symlink on PATH (the README's install step)."""

import os
import pathlib
import subprocess

import pytest

LAUNCHER = pathlib.Path(__file__).resolve().parents[1] / "bin" / "review-desk"


def test_launcher_runs_through_a_symlink_on_path(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "review-desk").symlink_to(LAUNCHER)
    # Like a user's shell: no active virtualenv, so nothing but PATH finds "review-desk".
    path = [d for d in os.environ["PATH"].split(os.pathsep) if ".venv" not in d]
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV" and not k.startswith("UV_")}
    env.update(PATH=os.pathsep.join([str(bindir), *path]), REVIEW_DESK_STATE=str(tmp_path / "state"))
    # The launcher's own environment (PEP 723) is built on first use and the launcher runs --offline, so warm it once, online.
    warm = subprocess.run(["uv", "run", "--quiet", "--script", str(LAUNCHER), "--help"], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=300)
    if warm.returncode != 0:
        pytest.skip(f"can't build the launcher's environment (offline with a cold uv cache?): {warm.stderr[-200:]}")
    r = subprocess.run(["review-desk", "--help"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-500:]
    assert "usage" in r.stdout.lower()


def test_launcher_is_a_pep723_script_whose_metadata_matches_pyproject():
    import re
    import tomllib
    text = LAUNCHER.read_text()
    first, second = text.splitlines()[:2]
    # `#!/usr/bin/env -S` isn't portable (busybox, GNU coreutils < 8.30): sh runs the quoted exec line, Python ignores it
    assert first == "#!/bin/sh" and second.startswith('"exec" "uv" "run"') and '"--script" "$0" "$@"' in second
    compile(text, "review-desk", "exec")  # and it is valid Python
    block = re.search(r"(?m)^# /// script$\n((?:#(?: .*)?\n)+?)# ///$", text)
    assert block, "no PEP 723 block"
    meta = tomllib.loads("".join(line[2:] if line.startswith("# ") else line[1:] for line in block[1].splitlines(True)))
    proj = tomllib.loads((LAUNCHER.parents[1] / "pyproject.toml").read_text())["project"]
    assert meta["dependencies"] == proj["dependencies"] and meta["requires-python"] == proj["requires-python"]


def test_launcher_puts_the_source_on_pythonpath_for_the_desks_it_starts():
    """`serve --background`, `open` and the reload supervisor run `python -m review_desk` in a new interpreter, which only
    finds the package through the PYTHONPATH the launcher exports (a real desk isn't started here; see CLAUDE.md)."""
    text = LAUNCHER.read_text()
    assert 'os.environ["PYTHONPATH"]' in text and "str(SRC)" in text and "sys.path.insert(0, str(SRC))" in text
    assert (LAUNCHER.parents[1] / "src" / "review_desk" / "__main__.py").is_file()
