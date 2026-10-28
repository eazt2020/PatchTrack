"""The guided uninstaller verifies what it owns before deleting anything.

These tests exercise manifest validation and the configuration cleanup rules.
They never stop containers, remove an account or touch the host.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

SPEC = importlib.util.spec_from_file_location(
    "patchtrack_uninstaller", ROOT / "scripts" / "patchtrack_uninstall.py"
)
assert SPEC and SPEC.loader
uninstaller = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = uninstaller
SPEC.loader.exec_module(uninstaller)


def write_manifest(tmp_path, **overrides):
    config = tmp_path / "etc" / "patchtrack"
    install = tmp_path / "opt" / "patchtrack"
    secrets = config / "secrets"
    secrets.mkdir(parents=True)
    install.mkdir(parents=True)
    data = {
        "config_dir": str(config),
        "install_dir": str(install),
        "secrets_dir": str(secrets),
        "runtime": "docker",
        "version": "3.0.0",
        "image": "patchtrack:3.0.0",
        "service_user": "patchtrack",
        "created_user": True,
        "created_group": True,
        "compose_project": "patchtrack",
    }
    data.update(overrides)
    path = config / "install.json"
    path.write_text(json.dumps(data))
    return path, config, install, secrets


def test_uninstaller_loads_the_manifest_written_by_the_installer(tmp_path):
    path, config, install, secrets = write_manifest(tmp_path)

    manifest = uninstaller.load_manifest(path)

    assert manifest.config_dir == config
    assert manifest.install_dir == install
    assert manifest.secrets_dir == secrets
    assert manifest.image == "patchtrack:3.0.0"
    assert manifest.service_user == "patchtrack"
    assert manifest.created_user is True
    assert manifest.created_group is True
    assert manifest.compose_project == "patchtrack"


def test_uninstaller_keeps_an_account_the_manifest_cannot_prove_it_owns(tmp_path):
    path, _, _, _ = write_manifest(tmp_path)
    data = json.loads(path.read_text())
    del data["created_user"]
    del data["created_group"]
    path.write_text(json.dumps(data))

    manifest = uninstaller.load_manifest(path)

    # Older manifests predate these flags. Removing a possibly pre-existing
    # account would be unsafe, so ownership defaults to false.
    assert manifest.created_user is False
    assert manifest.created_group is False
    assert manifest.service_user == "patchtrack"


def test_uninstaller_refuses_a_manifest_from_another_directory(tmp_path):
    path, _, _, _ = write_manifest(tmp_path)
    data = json.loads(path.read_text())
    data["config_dir"] = str(tmp_path / "somewhere-else")
    path.write_text(json.dumps(data))

    with pytest.raises(uninstaller.UninstallError, match="not its own directory"):
        uninstaller.load_manifest(path)


def test_uninstaller_refuses_an_image_it_does_not_own(tmp_path):
    path, _, _, _ = write_manifest(tmp_path, image="registry.example.com/other:latest")

    with pytest.raises(uninstaller.UninstallError, match="PatchTrack image"):
        uninstaller.load_manifest(path)


def test_uninstaller_refuses_overlapping_application_and_configuration(tmp_path):
    path, config, _, _ = write_manifest(tmp_path)
    data = json.loads(path.read_text())
    data["install_dir"] = str(config / "app")
    path.write_text(json.dumps(data))

    with pytest.raises(uninstaller.UninstallError, match="overlap"):
        uninstaller.load_manifest(path)


def test_uninstaller_refuses_a_symlinked_manifest(tmp_path):
    path, config, _, _ = write_manifest(tmp_path)
    alias = config / "install-link.json"
    alias.symlink_to(path)

    with pytest.raises(uninstaller.UninstallError, match="symbolic link"):
        uninstaller.load_manifest(alias)


def test_uninstaller_refuses_an_invalid_runtime_or_secrets_directory(tmp_path):
    path, config, _, _ = write_manifest(tmp_path)
    data = json.loads(path.read_text())
    data["runtime"] = "lxc"
    path.write_text(json.dumps(data))
    with pytest.raises(uninstaller.UninstallError, match="docker or podman"):
        uninstaller.load_manifest(path)

    data["runtime"] = "docker"
    data["secrets_dir"] = str(config / "credentials")
    path.write_text(json.dumps(data))
    with pytest.raises(uninstaller.UninstallError, match="secrets_dir"):
        uninstaller.load_manifest(path)


def test_uninstaller_preserves_operator_created_configuration(tmp_path):
    path, config, _, secrets = write_manifest(tmp_path)
    (config / ".env").write_text("PATCHTRACK_VERSION=3.0.0\n")
    for name in uninstaller.MANAGED_SECRETS:
        (secrets / name).write_text("generated\n")
    operator_note = config / "operator-note.txt"
    operator_note.write_text("keep me\n")
    operator_secret = secrets / "satellite.password"
    operator_secret.write_text("keep me too\n")
    manifest = uninstaller.load_manifest(path)

    removed, leftovers = uninstaller.cleanup_managed_config(manifest)

    assert not (config / ".env").exists()
    assert not path.exists()
    assert not any((secrets / name).exists() for name in uninstaller.MANAGED_SECRETS)
    assert operator_note.exists()
    assert operator_secret.exists()
    assert operator_note in leftovers
    assert operator_secret in leftovers
    assert removed


def test_uninstaller_removes_empty_configuration_directories(tmp_path):
    path, config, _, secrets = write_manifest(tmp_path)
    (config / ".env").write_text("PATCHTRACK_VERSION=3.0.0\n")
    manifest = uninstaller.load_manifest(path)

    uninstaller.cleanup_managed_config(manifest)

    assert not secrets.exists()
    assert not config.exists()


def test_uninstaller_only_removes_its_own_docker_proxy_dropin(tmp_path):
    ours = tmp_path / "ours.conf"
    ours.write_text(
        '[Service]\nEnvironment="HTTPS_PROXY=http://proxy:3128"\n'
        f"# {uninstaller.PROXY_MARKER}\n"
    )
    theirs = tmp_path / "theirs.conf"
    theirs.write_text('[Service]\nEnvironment="HTTPS_PROXY=http://other:3128"\n')

    assert uninstaller.proxy_dropin_is_ours(ours) is True
    assert uninstaller.proxy_dropin_is_ours(theirs) is False
    assert uninstaller.proxy_dropin_is_ours(tmp_path / "missing.conf") is False


def test_uninstaller_help_is_safe_to_run_without_root():
    import subprocess

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "patchtrack_uninstall.py"), "--help"],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0
    assert "--check" in result.stdout
    assert "--purge-data" in result.stdout
    assert "--keep-account" in result.stdout


def test_uninstaller_loads_an_airgap_manifest(tmp_path):
    bundle = tmp_path / "bundles" / "patchtrack-3.0.0"
    bundle.mkdir(parents=True)
    path, _, _, _ = write_manifest(
        tmp_path,
        installation_mode="airgap",
        airgap_bundle=str(bundle),
    )

    manifest = uninstaller.load_manifest(path)

    assert manifest.installation_mode == "airgap"
    assert manifest.airgap_bundle == bundle


def test_uninstaller_treats_a_manifest_without_a_mode_as_online(tmp_path):
    path, _, _, _ = write_manifest(tmp_path)

    manifest = uninstaller.load_manifest(path)

    assert manifest.installation_mode == "online"
    assert manifest.airgap_bundle is None


def test_uninstaller_rejects_an_unknown_installation_mode(tmp_path):
    path, _, _, _ = write_manifest(tmp_path, installation_mode="sneakernet")

    with pytest.raises(uninstaller.UninstallError, match="installation_mode"):
        uninstaller.load_manifest(path)


def test_uninstaller_rejects_an_invalid_airgap_bundle_path(tmp_path):
    path, _, _, _ = write_manifest(
        tmp_path, installation_mode="airgap", airgap_bundle="bundles/patchtrack"
    )

    with pytest.raises(uninstaller.UninstallError, match="airgap_bundle"):
        uninstaller.load_manifest(path)


def test_uninstaller_refuses_a_bundle_inside_the_application_directory(tmp_path):
    install = tmp_path / "opt" / "patchtrack"
    path, _, _, _ = write_manifest(
        tmp_path,
        installation_mode="airgap",
        airgap_bundle=str(install / "bundle"),
    )

    with pytest.raises(uninstaller.UninstallError, match="must not overlap"):
        uninstaller.load_manifest(path)
