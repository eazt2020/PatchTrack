"""The shipped systemd units must stay startable.

These units are the only delivery mechanism for a native (non-container)
installation, and nothing in the test suite would otherwise notice a typo that
only shows up when systemd refuses to start the service. The checks here are
structural: each instance must get a port, the environment must come from a
file, and the hardening that the documentation promises must actually be in
the unit.
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
    assert {
        "patchtrack@.service",
        "patchtrack-migrate.service",
        "patchtrack-tick.service",
        "patchtrack-tick.timer",
    } <= names


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


def test_the_tick_service_runs_the_shipped_script_as_the_job_user():
    service = read_unit("patchtrack-tick.service")["Service"]
    assert service["Type"] == "oneshot"
    assert service["User"] == "patchtrack"
    assert service["ExecStart"] == "/opt/patchtrack/scripts/patchtrack-tick.sh"
    for key, value in HARDENING.items():
        assert service.get(key) == value, f"{key} must be {value!r}"
    assert service["ReadWritePaths"].split() == ["/var/lib/patchtrack"]


def test_the_tick_service_waits_for_the_services_it_calls():
    unit = read_unit("patchtrack-tick.service")
    # The script calls these four directly; starting before them would turn
    # every request into a connection error on boot.
    for service in ("identity", "inventory", "compliance", "reporting"):
        assert f"patchtrack@{service}.service" in unit["Unit"]["After"]


def test_the_tick_service_loads_the_shared_environment():
    text = (UNIT_DIR / "patchtrack-tick.service").read_text()
    # The token must come from the same file the services use. No secret in
    # the unit itself — checked on the directives only, since the header
    # comment names the variable.
    directives = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "EnvironmentFile=/etc/patchtrack/.env\n" in text
    assert "PATCHTRACK_SERVICE_TOKEN" not in directives
    assert "Environment=PATCHTRACK_SERVICE_TOKEN" not in directives


def test_the_timer_fires_hourly_and_survives_a_reboot():
    timer = read_unit("patchtrack-tick.timer")
    assert timer["Timer"]["OnCalendar"] == "hourly"
    assert timer["Timer"]["Persistent"] == "true"
    assert timer["Timer"]["Unit"] == "patchtrack-tick.service"
    assert timer["Install"]["WantedBy"] == "timers.target"


def test_the_tick_script_covers_every_service_port():
    script = (UNIT_DIR.parents[1] / "scripts" / "patchtrack-tick.sh").read_text()
    for service in SERVICES:
        assert f"{service}) port=" in script, f"{service} has no port fallback"
