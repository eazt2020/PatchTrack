"""The scheduled-job runners must use a service credential, not a human one.

A timer has no user session. If these scripts ever grew a password or a
gateway cookie, scheduling would silently require a person's account. They are
checked structurally because the real ones need a running deployment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
RUNNERS = ["patchtrack-job.sh", "patchtrack-tick.sh"]


@pytest.mark.parametrize("name", RUNNERS)
def test_the_script_exists_and_is_executable(name: str):
    path = SCRIPTS / name
    assert path.is_file()
    assert path.stat().st_mode & 0o111, f"{name} is not executable"


def test_the_job_script_authenticates_as_the_scheduler():
    text = (SCRIPTS / "patchtrack-job.sh").read_text()
    assert '"x-patchtrack-internal"' in text or "'x-patchtrack-internal'" in text
    assert "PATCHTRACK_SERVICE_TOKEN" in text
    assert '"scheduler"' in text or "'scheduler'" in text


def test_the_tick_script_delegates_to_the_job_script():
    # The tick runner owns due-state only. Credentials, ports and the
    # container/native choice live in one place, so the systemd timer works
    # for both deployments.
    text = (SCRIPTS / "patchtrack-tick.sh").read_text()
    assert "patchtrack-job.sh" in text
    assert 'JOB="${PATCHTRACK_JOB_SCRIPT:-$ROOT/scripts/patchtrack-job.sh}"' in text
    assert "x-patchtrack-internal" not in text


def executable_lines(name: str) -> str:
    """The script without comments, so prose about credentials is not a hit."""
    lines = (SCRIPTS / name).read_text().splitlines()
    return "\n".join(
        line for line in lines if not line.lstrip().startswith("#")
    ).lower()


@pytest.mark.parametrize("name", RUNNERS)
def test_the_script_never_uses_a_human_credential(name: str):
    # No password prompt, no browser cookie, no gateway /api route.
    text = executable_lines(name)
    assert "password" not in text
    assert "patchtrack_session" not in text
    assert "/api/" not in text


def test_the_job_script_fails_on_a_reported_failure():
    text = (SCRIPTS / "patchtrack-job.sh").read_text()
    assert '"status"' in text and '"failed"' in text


def test_the_tick_script_fails_when_a_step_fails():
    # It does not parse the response itself; it trusts the job script's exit
    # status and turns any failure into its own non-zero exit.
    text = (SCRIPTS / "patchtrack-tick.sh").read_text()
    assert '"$JOB" "$@"' in text
    assert 'exit "$FAILED"' in text


def test_the_job_script_works_in_both_deployments():
    # The same script has to serve a compose installation and a systemd one;
    # a container-only runner silently breaks the native deployment.
    text = (SCRIPTS / "patchtrack-job.sh").read_text()
    assert "PATCHTRACK_MODE" in text
    assert "docker compose" in text            # container transport
    assert '127.0.0.1:$(port_for' in text      # native transport
    assert "/etc/patchtrack/ports/" in text    # native port lookup


def test_the_container_transport_supports_podman():
    # RHEL and Rocky ship Podman, and the deployment guide uses
    # podman-compose. A runner that only knows `docker compose` would fall
    # back to the native transport there and then fail to reach a port that
    # is not published — a silent break of the container deployment.
    text = (SCRIPTS / "patchtrack-job.sh").read_text()
    assert "podman-compose" in text
    assert "com.docker.compose.service" in text   # label lookup works in both
    assert "PATCHTRACK_CONTAINER_CLI" in text


def test_the_container_transport_never_needs_a_host_credential():
    text = (SCRIPTS / "patchtrack-job.sh").read_text()
    # The token and PORT are read inside the container, from its env_file.
    assert 'os.environ["PATCHTRACK_SERVICE_TOKEN"]' in text
    assert "exec -T" in text


def test_the_native_transport_isolates_the_interpreter():
    text = (SCRIPTS / "patchtrack-job.sh").read_text()
    assert '"$PYTHON" -I -' in text


def test_the_tick_script_writes_a_stamp_only_after_success():
    text = (SCRIPTS / "patchtrack-tick.sh").read_text()
    # The stamp call sits inside the success branch of due_run, so a failed
    # collection is retried on the next tick instead of waiting a full
    # interval.
    runner = text[text.index("due_run() {"):text.index("collection() {")]
    assert runner.index('"$JOB" "$@"') < runner.index("stamp ")
    assert runner.index("stamp ") > runner.index("then")


def test_the_tick_script_isolates_a_failing_step():
    text = (SCRIPTS / "patchtrack-tick.sh").read_text()
    # A dead source system must not stop the daily snapshot or the report
    # tick: every step is run through step(), which records the failure and
    # continues.
    assert "FAILED=1" in text
    assert "exit \"$FAILED\"" in text
