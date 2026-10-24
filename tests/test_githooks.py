import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / ".githooks"


@pytest.fixture
def stub(tmp_path):
    """A PATH holding only a fake systemctl that logs its arguments; `cat` succeeds only for the unit named in UNITS."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls"
    sc = bin_dir / "systemctl"
    sc.write_text(f'#!/bin/sh\necho "$@" >> {log}\n'
                  '[ "$2" = cat ] && { case " $UNITS " in *" $3 "*) exit 0;; *) exit 1;; esac; }\nexit 0\n')
    sc.chmod(sc.stat().st_mode | stat.S_IXUSR)
    return bin_dir, log


def run_hook(name, *args, path, **env):
    """PATH is the given dir plus `basename` only, so a real systemctl on this machine is never reached."""
    tools = Path(path).parent / "tools"
    tools.mkdir(exist_ok=True)
    if not (tools / "basename").exists():
        (tools / "basename").symlink_to(shutil.which("basename"))
    e = {"PATH": f"{path}:{tools}", "HOME": "/nonexistent", **env}
    return subprocess.run([str(HOOKS / name), *args], env=e, capture_output=True, text=True, stdin=subprocess.DEVNULL)


def calls(log):
    return log.read_text().splitlines() if log.exists() else []


def test_hooks_are_symlinks_to_an_executable_script():
    script = HOOKS / "restart-systemd-user-unit"
    assert os.access(script, os.X_OK)
    for name in ("post-merge", "post-rewrite"):
        assert (HOOKS / name).is_symlink() and (HOOKS / name).resolve() == script.resolve()


def test_post_merge_restarts_the_unit(stub):
    bin_dir, log = stub
    r = run_hook("post-merge", "0", path=bin_dir, UNITS="review-desk.service")
    assert r.returncode == 0 and "restarted systemd user unit review-desk.service" in r.stdout
    assert calls(log) == ["--user cat review-desk.service", "--user restart review-desk.service"]


def test_post_rewrite_restarts_only_after_a_rebase(stub):
    bin_dir, log = stub
    assert run_hook("post-rewrite", "amend", path=bin_dir, UNITS="review-desk.service").returncode == 0
    assert calls(log) == []
    assert run_hook("post-rewrite", "rebase", path=bin_dir, UNITS="review-desk.service").returncode == 0
    assert calls(log)[-1] == "--user restart review-desk.service"


def test_unit_name_can_be_overridden(stub):
    bin_dir, log = stub
    run_hook("post-merge", "0", path=bin_dir, UNITS="desk2.service", REVIEW_DESK_UNIT="desk2.service")
    assert calls(log)[-1] == "--user restart desk2.service"


def test_missing_unit_is_a_quiet_no_op(stub):
    bin_dir, log = stub
    r = run_hook("post-merge", "0", path=bin_dir, UNITS="")
    assert r.returncode == 0 and r.stdout == "" and calls(log) == ["--user cat review-desk.service"]


def test_no_systemctl_is_a_quiet_no_op(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    r = run_hook("post-merge", "0", path=empty)
    assert r.returncode == 0 and r.stdout == ""


def test_a_failed_restart_does_not_fail_the_pull(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sc = bin_dir / "systemctl"
    sc.write_text('#!/bin/sh\n[ "$2" = restart ] && exit 1\nexit 0\n')
    sc.chmod(0o755)
    r = run_hook("post-merge", "0", path=bin_dir)
    assert r.returncode == 0 and "restarted" not in r.stdout
