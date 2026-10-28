"""The shipped systemd units must stay startable.

These units are the only delivery mechanism for a native (non-container)
installation, and nothing in the test suite would otherwise notice a typo that
only shows up when systemd refuses to start the service. The checks here are
structural: each instance must get a port, the environment must come from a
file, and the hardening that the documentation promises must actually be in
the unit.

Scheduled work is not a systemd unit. Each service runs its own in-app
scheduler, so there is deliberately no timer here.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

UNIT_DIR = Path(__file__).resolve().parents[2] / "deploy" / "systemd"
SERVICES = ("identity", "inventory", "compliance", "reporting", "gateway")

HARDENING = {
    "NoNewPrivileges": "true",
    "PrivateTmp": "true",
    "ProtectSystem": "strict",
    "ProtectHome": "true",
    "CapabilityBoundingSet": "",
    "AmbientCapabilities": "",
}


def read_unit(name: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # systemd directives are case-sensitive
    parser.read(UNIT_DIR / name)
    return parser


def test_the_expected_units_are_present():
    names = {p.name for p in UNIT_DIR.iterdir()}
    assert {"patchtrack@.service", "patchtrack-migrate.service"} <= names


def test_no_scheduler_timer_or_tick_unit_is_shipped():
    # Scheduling belongs to the service process. A leftover timer would run
    # the jobs a second time, competing for the same due state.
    names = {p.name for p in UNIT_DIR.iterdir()}
    assert "patchtrack-tick.service" not in names
    assert "patchtrack-tick.timer" not in names


def test_each_service_instance_gets_its_own_port():
    unit = read_unit("patchtrack@.service")
    env_files = unit["Service"]["EnvironmentFile"].split()
    assert "/etc/patchtrack/ports/%i" in env_files, (
        "the per-instance port file must be loaded, or every instance binds "
        "the same port"
    )
    assert "PORT=" not in unit["Service"].get("Environment", ""), (
        "a hard-coded PORT would be shared by all five instances"
    )


def test_the_instance_name_selects_the_service_directory():
    exec_start = read_unit("patchtrack@.service")["Service"]["ExecStart"]
    assert "/opt/patchtrack/services/%i" in exec_start
    assert "--app-dir" in exec_start


def test_migrations_run_before_every_instance():
    unit = read_unit("patchtrack@.service")
    assert "patchtrack-migrate.service" in unit["Unit"]["Requires"]
    assert "patchtrack-migrate.service" in unit["Unit"]["After"]


def test_the_service_never_runs_as_root():
    service = read_unit("patchtrack@.service")["Service"]
    assert service["User"] == "patchtrack"
    for key, value in HARDENING.items():
        assert service.get(key) == value, f"{key} must be {value!r}"


def test_the_service_may_only_write_its_data_directory():
    service = read_unit("patchtrack@.service")["Service"]
    assert service["ReadWritePaths"].split() == ["/var/lib/patchtrack"]


def test_the_environment_is_not_hard_coded():
    # A credential in the unit would be committed; it must come from a file.
    # Read the raw text: EnvironmentFile is given twice (shared environment,
    # then the per-instance port) and configparser keeps only the last.
    text = (UNIT_DIR / "patchtrack@.service").read_text()
    assert "PATCHTRACK_JWT_SECRET" not in text
    assert "EnvironmentFile=/etc/patchtrack/.env\n" in text


def test_the_forwarded_allow_ips_is_configurable_not_wildcarded():
    exec_start = read_unit("patchtrack@.service")["Service"]["ExecStart"]
    assert "--forwarded-allow-ips ${PATCHTRACK_FORWARDED_IPS}" in exec_start
    assert "--forwarded-allow-ips *" not in exec_start


@pytest.mark.parametrize("name", ["patchtrack@.service", "patchtrack-migrate.service"])
def test_units_declare_an_install_section(name: str):
    assert read_unit(name)["Install"]["WantedBy"] == "multi-user.target"


def test_the_migration_unit_is_a_oneshot():
    service = read_unit("patchtrack-migrate.service")["Service"]
    assert service["Type"] == "oneshot"
    assert "patchtrack_common.migrate" in service["ExecStart"]


def test_the_shipped_units_have_no_placeholder_port():
    # Every instance must load /etc/patchtrack/ports/%i; a port baked into the
    # unit would be shared by all five.
    text = (UNIT_DIR / "patchtrack@.service").read_text()
    assert "Environment=PORT=" not in text


def test_the_job_script_covers_every_service_port():
    # The manual job runner owns the port map for native installations.
    script = (UNIT_DIR.parents[1] / "scripts" / "patchtrack-job.sh").read_text()
    for service in SERVICES:
        assert f"{service}) port=" in script, f"{service} has no port fallback"
