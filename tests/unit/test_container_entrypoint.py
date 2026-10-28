"""The container entrypoint must run the command compose gives it.

Compose overrides CMD, not ENTRYPOINT, so a service that supplies a command
(the one-shot `migrate` job does) reaches this script as arguments. If the
script ignores them it starts uvicorn instead — with no PATCHTRACK_SERVICE,
which exits non-zero, and every other service waits for migrate to succeed.
That failure only appears when the stack is actually run, so it is pinned
here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

CONTAINER = Path(__file__).resolve().parents[2] / "deploy" / "container"
ENTRYPOINT = CONTAINER / "entrypoint.sh"
DOCKERFILE = CONTAINER / "Dockerfile"


def test_a_supplied_command_is_executed():
    # /bin/echo stands in for `python -m patchtrack_common.migrate`. No
    # PATCHTRACK_SERVICE is set: the command must not need one.
    result = subprocess.run(
        [str(ENTRYPOINT), "/bin/echo", "migrated"],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "migrated"


def test_a_missing_service_is_still_refused():
    # With no command and no PATCHTRACK_SERVICE the script must fail loudly
    # rather than guess which service it is.
    result = subprocess.run(
        [str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert result.returncode != 0
    assert "PATCHTRACK_SERVICE" in result.stderr


def test_the_log_level_is_lowercased():
    # The documented setting is written in capitals (INFO) but uvicorn only
    # accepts lowercase names, and exits if given anything else.
    text = ENTRYPOINT.read_text()
    assert "--log-level \"$LOG_LEVEL\"" in text
    assert "tr '[:upper:]' '[:lower:]'" in text


def test_the_service_starts_through_the_tls_capable_launcher():
    # Plain `uvicorn` cannot reload a certificate uploaded in Settings, so
    # every service must start through the wrapper, with its own name exported
    # for the launcher's gateway-only TLS decision.
    text = ENTRYPOINT.read_text()
    assert 'export PATCHTRACK_SERVICE="$SERVICE"' in text
    assert 'python -m patchtrack_common.serve "app.main:app"' in text
    assert not text.rstrip().endswith("uvicorn app.main:app")


def test_the_build_elevates_only_for_the_ownership_step():
    # Red Hat's UBI python image sets USER 1001, so the chmod/chown step fails
    # without an explicit USER root. The image must still finish as 1001.
    text = DOCKERFILE.read_text()
    chown = text.index("RUN chmod")
    assert text.index("USER root") < chown
    assert text.rindex("USER 1001") > chown


def test_the_exec_branch_precedes_the_service_check():
    text = ENTRYPOINT.read_text()
    assert text.index('exec "$@"') < text.index("PATCHTRACK_SERVICE:?"), (
        "the command branch must run before the service check, or the migrate "
        "job is rejected for having no service"
    )
