"""The scheduled-job runners must use a service credential, not a human one.

A scheduled job has no user session. The in-app scheduler calls the service
functions directly, but the manual runner still ships for operators; if it
ever grew a password or a gateway cookie, running a job would silently
require a person's account. It is checked structurally because the real one
needs a running deployment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
RUNNERS = ["patchtrack-job.sh"]


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


