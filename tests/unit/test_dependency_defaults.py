"""Default container images must be supported and pinned.

An end-of-life database or dashboard line stops receiving security fixes, and
a floating `:latest` means two builds of the same commit can differ. These are
deployment defects that no service-level test can see.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = (ROOT / "docker-compose.yml").read_text()
DOCKERFILE = (ROOT / "deploy" / "container" / "Dockerfile").read_text()


def _image(service_block: str) -> str:
    match = re.search(r"^\s+image:\s*(.+)$", service_block, re.MULTILINE)
    assert match, "service has no image"
    return match.group(1)


def _major(image: str) -> int:
    # Compose defaults look like ${POSTGRES_IMAGE:-docker.io/...:17}.
    image = image.rstrip("}").rsplit(":-", 1)[-1]
    tag = image.rsplit(":", 1)[-1]
    return int(tag.split(".")[0])


def test_postgres_default_is_a_supported_major():
    image = _image(COMPOSE[COMPOSE.index("  postgres:"):])
    assert _major(image) >= 16, f"PostgreSQL default is {image}"


def test_postgres_13_is_not_a_documented_default():
    assert "library/postgres:13" not in COMPOSE


def test_the_deployment_does_not_ship_grafana():
    """Grafana is not part of PatchTrack; the SPA is the only dashboard."""
    assert "grafana:" not in COMPOSE
    assert "grafana-data" not in COMPOSE
    assert not (ROOT / "deploy" / "grafana").exists()


def test_python_base_image_is_pinned():
    match = re.search(r"^ARG PYTHON_IMAGE=(\S+)$", DOCKERFILE, re.MULTILINE)
    assert match
    image = match.group(1)
    assert not image.endswith(":latest"), image
    assert re.search(r"@sha256:|:\d+-\d+", image), image


# --------------------------------------------------------------- monitoring
MONITORING = ROOT / "deploy" / "monitoring"


def test_prometheus_scrapes_every_service():
    import yaml

    config = yaml.safe_load((MONITORING / "prometheus.yml").read_text())
    targets = set(config["scrape_configs"][0]["static_configs"][0]["targets"])
    assert targets == {
        "identity:8001", "inventory:8002", "compliance:8003",
        "reporting:8004",
    }
    # The gateway's listener is published, so it must not serve /metrics.
    assert "gateway:8005" not in targets


def test_alert_rules_are_valid_and_cover_availability():
    import yaml

    rules = yaml.safe_load((MONITORING / "alerts.yml").read_text())
    alerts = {rule["alert"] for group in rules["groups"] for rule in group["rules"]}
    assert "PatchTrackServiceDown" in alerts
    assert "PatchTrackHighErrorRate" in alerts


def _direct_pins(text: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#")[0].strip().rstrip("\\").strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==([^;\s]+)", line)
        if match:
            pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def test_the_hashed_lock_matches_the_direct_pins():
    """The image installs the lock; a stale lock would ship old packages."""
    requirements = (ROOT / "deploy" / "container" / "requirements.txt").read_text()
    lock = (ROOT / "deploy" / "container" / "requirements.lock.txt").read_text()
    direct = _direct_pins(requirements)
    locked = _direct_pins(lock)
    assert direct, "requirements.txt has no pins"
    for name, version in direct.items():
        assert locked.get(name) == version, (
            f"{name}=={version} in requirements.txt but "
            f"{locked.get(name)!r} in requirements.lock.txt; regenerate the lock"
        )


def test_the_image_installs_the_hashed_lock():
    assert "--require-hashes" in DOCKERFILE
    assert "requirements.lock.txt" in DOCKERFILE


def test_prometheus_image_is_pinned_and_not_latest():
    assert "prom/prometheus:v3.15.1" in COMPOSE
    assert "prom/prometheus:latest" not in COMPOSE
