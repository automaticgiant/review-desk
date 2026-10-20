"""bin/review-desk must work when installed as a symlink on PATH (the README's install step)."""

import os
import pathlib
import subprocess

LAUNCHER = pathlib.Path(__file__).resolve().parents[1] / "bin" / "review-desk"


def test_launcher_runs_through_a_symlink_on_path(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "review-desk").symlink_to(LAUNCHER)
    # Like a user's shell: no active virtualenv, so nothing but PATH finds "review-desk".
    path = [d for d in os.environ["PATH"].split(os.pathsep) if ".venv" not in d]
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV" and not k.startswith("UV_")}
    env.update(PATH=os.pathsep.join([str(bindir), *path]), REVIEW_DESK_STATE=str(tmp_path / "state"))
    r = subprocess.run(["review-desk", "--help"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-500:]
    assert "usage" in r.stdout.lower()
