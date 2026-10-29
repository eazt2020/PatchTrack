"""The maintenance wrappers leave a complete, private run log.

The helper is deliberately a shell script so it captures output the Python
tools do not print themselves (dnf, the container runtime, psql). These tests
run it as a subprocess so the file descriptor handling is exercised for real
without touching the test process's stdout.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts" / "patchtrack-run-logged.sh"


def _run(tmp_path: Path, *argv: str, env: dict | None = None):
    base_env = os.environ.copy()
    base_env["PATCHTRACK_LOG_DIR"] = str(tmp_path / "logs")
    base_env.pop("XDG_STATE_HOME", None)
    if env:
        base_env.update(env)
    return subprocess.run(
        ["bash", str(HELPER), *argv],
        env=base_env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_the_tool_exit_status_is_preserved_and_all_output_is_logged(tmp_path):
    result = _run(
        tmp_path, "install",
        "/bin/sh", "-c", "echo stdout-line; echo stderr-line >&2; exit 3",
    )
    assert result.returncode == 3
    assert "stdout-line" in result.stdout
    assert "stderr-line" in result.stdout

    logs = list((tmp_path / "logs").glob("install-*.log"))
    assert len(logs) == 1
    text = logs[0].read_text()
    assert "PatchTrack install run log" in text
    assert "stdout-line" in text
    assert "stderr-line" in text
    assert "Exit status     : 3" in text
    assert "Started (UTC)" in text and "Finished (UTC)" in text

    mode = logs[0].stat().st_mode & 0o777
    assert mode == 0o600
    link = tmp_path / "logs" / "install.log"
    assert link.is_symlink()
    assert os.readlink(link) == logs[0].name
    assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o750


def test_a_failed_log_directory_falls_back_to_the_user_state_directory(tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("not a directory")
    state = tmp_path / "state"
    result = _run(
        tmp_path,
        "upgrade", "/bin/sh", "-c", "echo ran; exit 0",
        env={
            "PATCHTRACK_LOG_DIR": str(blocked / "logs"),
            "XDG_STATE_HOME": str(state),
        },
    )
    assert result.returncode == 0
    assert "ran" in result.stdout
    assert "cannot write" in result.stderr
    fallback = list((state / "patchtrack" / "logs").glob("upgrade-*.log"))
    assert len(fallback) == 1
    assert "Exit status     : 0" in fallback[0].read_text()


def test_old_runs_are_pruned_but_other_files_are_kept(tmp_path):
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    old = log_dir / "uninstall-20200101T000000Z-1.log"
    old.write_text("old\n")
    keep = log_dir / "operator-notes.log"
    keep.write_text("keep\n")
    ancient = time.time() - 120 * 86400
    os.utime(old, (ancient, ancient))

    result = _run(
        tmp_path, "uninstall", "/bin/sh", "-c", "echo done",
        env={"PATCHTRACK_LOG_RETENTION_DAYS": "90"},
    )
    assert result.returncode == 0
    assert not old.exists()
    assert keep.exists()


def test_the_helper_requires_a_tool_and_a_command():
    result = subprocess.run(
        ["bash", str(HELPER)], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "usage:" in result.stderr


@pytest.mark.parametrize(
    ("wrapper", "tool"),
    [
        ("install.sh", "install"),
        ("upgrade.sh", "upgrade"),
        ("uninstall.sh", "uninstall"),
    ],
)
def test_each_wrapper_runs_the_python_tool_through_the_logger(wrapper, tool):
    text = (ROOT / wrapper).read_text()
    assert "patchtrack-run-logged.sh" in text
    assert f"patchtrack-run-logged.sh\" {tool}" in text
    assert f"patchtrack_{tool}.py" in text
