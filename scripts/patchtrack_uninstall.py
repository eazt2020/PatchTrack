#!/usr/bin/env python3
"""Guided PatchTrack uninstaller for RHEL 9 / Rocky 9.

The uninstaller reads the manifest written by the guided installer
(``install.json``), checks what is actually present, removes only the things
that installation owns, and verifies the result:

1. preflight: root, manifest, paths, runtime, compose project, image, account,
   data volumes and configuration leftovers;
2. removal: containers and networks, optionally data volumes, the application
   image, the application tree and the installer-managed configuration files;
3. account removal only when nothing the account owns is left behind;
4. postcheck: the project, image, paths and account are gone, and any data
   volumes that were preserved are reported.

Data volumes are preserved unless ``--purge-data`` is given explicitly. A
configuration file the installer did not create is never deleted; it is
reported so an operator can keep or remove it deliberately. There is no
rollback: an uninstall deletes state, so every step is checked before it runs.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# The uninstaller reuses the installer's command, check and path helpers so
# both tools agree on protected paths and runtime detection.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import patchtrack_install as base  # noqa: E402

COMPOSE_PROJECT = "patchtrack"
MANAGED_SECRETS = ("postgres_password", "database_url", "jwt_secret", "service_token")
MANAGED_CONFIG_FILES = ("install.json", ".env")
PROXY_DROPIN = base.DOCKER_PROXY_DROPIN
PROXY_MARKER = "Managed by the PatchTrack guided installer"
PROTECTED_ACCOUNT_NAMES = {"root", "nobody"}


class UninstallError(RuntimeError):
    """A condition that must stop the uninstall before or during removal."""


class UserCancelled(UninstallError):
    """The operator chose not to continue."""


@dataclass
class Manifest:
    path: Path
    config_dir: Path
    install_dir: Path
    secrets_dir: Path
    runtime: str
    image: str
    service_user: str = ""
    service_group: str = ""
    created_user: bool = False
    created_group: bool = False
    compose_project: str = COMPOSE_PROJECT
    version: str = ""
    installation_mode: str = "online"
    airgap_bundle: Optional[Path] = None

    @property
    def env_file(self) -> Path:
        return self.config_dir / ".env"


@dataclass
class Options:
    check: bool = False
    yes: bool = False
    purge_data: bool = False
    keep_image: bool = False
    keep_account: bool = False
    remove_proxy_config: bool = False
    config_dir: Path = base.DEFAULT_CONFIG_DIR


@dataclass
class RuntimeState:
    ready: bool = False
    detail: str = ""
    containers: List[str] = field(default_factory=list)
    networks: List[str] = field(default_factory=list)
    volumes: List[str] = field(default_factory=list)
    image_present: bool = False


# --------------------------------------------------------------------- helpers

def _ask_yes_no(label: str, default: bool = False) -> bool:
    marker = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"    {label} [{marker}]: ").strip().lower()
        except EOFError as exc:
            raise UserCancelled(
                "no input available; rerun interactively or use --yes"
            ) from exc
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        base.warn("please answer yes or no")


def _absolute(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.startswith("/"):
        raise UninstallError(f"{label} in install.json must be an absolute path")
    path = base.clean_path(value)
    problem = base._path_error(str(path))
    if problem:
        raise UninstallError(f"{label} {problem}")
    return path


def load_manifest(path: Path) -> Manifest:
    if path.is_symlink():
        raise UninstallError(f"{path} is a symbolic link; refusing to trust it")
    if not path.is_file():
        raise UninstallError(
            f"{path} was not found. Point --config-dir at the configuration "
            "directory chosen during installation."
        )
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise UninstallError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise UninstallError(f"{path} is not an installation manifest")

    config_dir = _absolute(data.get("config_dir"), "config_dir")
    install_dir = _absolute(data.get("install_dir"), "install_dir")
    secrets_dir = _absolute(data.get("secrets_dir"), "secrets_dir")
    if config_dir.resolve() != path.parent.resolve():
        raise UninstallError(
            f"{path} records config_dir {config_dir}, which is not its own directory"
        )
    if secrets_dir != config_dir / "secrets":
        raise UninstallError(
            f"secrets_dir in install.json must be {config_dir / 'secrets'}"
        )
    if config_dir == install_dir or base.paths_overlap(config_dir, install_dir):
        raise UninstallError("the application and configuration directories overlap")
    for label, value in (
        ("install directory", install_dir),
        ("configuration directory", config_dir),
    ):
        problem = base.validate_install_path(str(value), label)
        if problem:
            raise UninstallError(problem)

    runtime = data.get("runtime")
    if runtime not in ("docker", "podman"):
        raise UninstallError("runtime in install.json must be docker or podman")

    version = str(data.get("version") or "")
    image = str(data.get("image") or (f"patchtrack:{version}" if version else ""))
    if not re.fullmatch(r"patchtrack:[A-Za-z0-9][A-Za-z0-9._-]*", image):
        raise UninstallError(
            "image in install.json must be a PatchTrack image (patchtrack:<version>)"
        )

    service_user = str(data.get("service_user") or "")
    if service_user:
        problem = base.validate_service_user(service_user)
        if problem:
            raise UninstallError(f"service_user in install.json {problem}")
        if service_user in PROTECTED_ACCOUNT_NAMES:
            raise UninstallError("refusing to manage a system account")

    project = str(data.get("compose_project") or COMPOSE_PROJECT)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise UninstallError("compose_project in install.json is not a valid project name")

    # Manifests written before air-gap support have no mode and are online.
    mode = str(data.get("installation_mode") or "online")
    if mode not in ("online", "airgap"):
        raise UninstallError(
            "installation_mode in install.json must be online or airgap"
        )
    bundle_value = data.get("airgap_bundle")
    if bundle_value is None:
        bundle = None
    elif not isinstance(bundle_value, str) or not bundle_value.startswith("/"):
        raise UninstallError("airgap_bundle in install.json must be an absolute path")
    else:
        bundle = base.clean_path(bundle_value)
        problem = base._path_error(str(bundle))
        if problem:
            raise UninstallError(f"airgap_bundle {problem}")
        if base.paths_overlap(bundle, install_dir) or base.paths_overlap(
            bundle, config_dir
        ):
            raise UninstallError(
                "airgap_bundle in install.json must not overlap the application "
                "or configuration directory"
            )
    return Manifest(
        path=path,
        config_dir=config_dir,
        install_dir=install_dir,
        secrets_dir=secrets_dir,
        runtime=runtime,
        image=image,
        service_user=service_user,
        service_group=service_user,
        # A manifest written before these flags existed cannot prove ownership,
        # so the safe default is "do not remove the account".
        created_user=data.get("created_user") is True,
        created_group=data.get("created_group") is True,
        compose_project=project,
        version=version,
        installation_mode=mode,
        airgap_bundle=bundle,
    )


def infer_service_user(manifest: Manifest) -> str:
    """Fall back for manifests written before service_user was recorded."""
    if manifest.service_user:
        return manifest.service_user
    try:
        owner = manifest.install_dir.stat().st_uid
        record = pwd.getpwnam("patchtrack")
    except (OSError, KeyError):
        return ""
    return "patchtrack" if record.pw_uid == owner else ""


def compose_command(manifest: Manifest) -> List[str]:
    return ["docker", "compose"] if manifest.runtime == "docker" else ["podman-compose"]


def compose_ready(manifest: Manifest) -> Tuple[bool, str]:
    if manifest.runtime == "docker":
        if not base.command_exists("docker"):
            return False, "the docker command is not available"
        result = base.run_command(
            ["docker", "compose", "version"], capture=True, check=False
        )
        if result.returncode != 0:
            return False, "Docker Compose v2 is not available"
        return True, "docker compose is available"
    if not base.command_exists("podman-compose"):
        return False, "the podman-compose command is not available"
    result = base.run_command(["podman-compose", "--version"], capture=True, check=False)
    if result.returncode != 0:
        return False, "podman-compose is not usable"
    return True, "podman-compose is available"


def _project_items(manifest: Manifest, kind: str) -> Optional[List[str]]:
    label = f"label=com.docker.compose.project={manifest.compose_project}"
    if kind == "containers":
        command = ["docker", "ps", "-a"] if manifest.runtime == "docker" else [
            "podman", "ps", "-a"
        ]
    elif kind == "networks":
        command = ["docker", "network", "ls"] if manifest.runtime == "docker" else [
            "podman", "network", "ls"
        ]
    else:
        command = ["docker", "volume", "ls"] if manifest.runtime == "docker" else [
            "podman", "volume", "ls"
        ]
    command += ["--filter", label, "--format", "{{.ID}}" if kind == "containers" else "{{.Name}}"]
    result = base.run_command(command, capture=True, check=False)
    if result.returncode != 0:
        return None
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def inspect_runtime(manifest: Manifest) -> RuntimeState:
    state = RuntimeState()
    ready, detail = compose_ready(manifest)
    state.ready = ready
    state.detail = detail
    if not ready:
        return state
    state.containers = _project_items(manifest, "containers") or []
    state.networks = _project_items(manifest, "networks") or []
    state.volumes = _project_items(manifest, "volumes") or []
    state.image_present = base.image_exists(manifest.runtime, manifest.image)
    return state


def unknown_config_entries(manifest: Manifest) -> Tuple[List[Path], List[Path]]:
    """Files the installer did not create, at both levels of the config tree."""
    managed = set(MANAGED_CONFIG_FILES) | {"secrets"}
    top: List[Path] = []
    if manifest.config_dir.is_dir():
        for entry in sorted(manifest.config_dir.iterdir(), key=lambda item: item.name):
            if entry.name not in managed:
                top.append(entry)
    managed_secrets = set(MANAGED_SECRETS)
    secret: List[Path] = []
    if manifest.secrets_dir.is_dir():
        for entry in sorted(manifest.secrets_dir.iterdir(), key=lambda item: item.name):
            if entry.name not in managed_secrets:
                secret.append(entry)
    return top, secret


def proxy_dropin_is_ours(path: Path) -> bool:
    try:
        return path.is_file() and PROXY_MARKER in path.read_text()
    except OSError:
        return False


def cleanup_managed_config(manifest: Manifest) -> Tuple[List[Path], List[Path]]:
    """Delete only installer-created files; return (removed, leftovers)."""
    removed: List[Path] = []
    for path in (manifest.env_file, *(manifest.secrets_dir / name for name in MANAGED_SECRETS)):
        if path.is_symlink() or path.is_file():
            path.unlink()
            removed.append(path)
    # The manifest is removed last so a failure earlier leaves it available.
    if manifest.path.is_file() and not manifest.path.is_symlink():
        manifest.path.unlink()
        removed.append(manifest.path)
    for directory in (manifest.secrets_dir, manifest.config_dir):
        if not directory.exists():
            continue
        try:
            directory.rmdir()
            removed.append(directory)
        except OSError:
            pass
    top, secret = unknown_config_entries(manifest)
    return removed, top + secret


# ------------------------------------------------------------------ preflight

def collect_preflight(manifest: Manifest, options: Options) -> Tuple[List[base.Check], RuntimeState]:
    checks: List[base.Check] = []

    checks.append(base.Check(
        "root privileges",
        os.geteuid() == 0,
        "running as root" if os.geteuid() == 0 else "rerun with sudo",
    ))

    checks.append(base.Check(
        "installation manifest",
        manifest.path.is_file(),
        str(manifest.path),
    ))

    if manifest.installation_mode == "airgap":
        detail = "air-gapped installation"
        if manifest.airgap_bundle:
            detail += f"; bundle at {manifest.airgap_bundle} is not removed"
        checks.append(base.Check("installation mode", True, detail))
    else:
        checks.append(base.Check("installation mode", True, "online installation"))

    for label, path in (
        ("application directory", manifest.install_dir),
        ("configuration directory", manifest.config_dir),
    ):
        if path.is_symlink():
            checks.append(base.Check(label, False, f"{path} is a symbolic link"))
        elif not path.exists():
            checks.append(base.Check(label, False, f"{path} does not exist"))
        elif not path.is_dir():
            checks.append(base.Check(label, False, f"{path} is not a directory"))
        else:
            checks.append(base.Check(label, True, str(path)))

    compose_present = (
        (manifest.install_dir / "docker-compose.yml").is_file()
        and (manifest.install_dir / "docker-compose.secrets.yml").is_file()
    )
    checks.append(base.Check(
        "application identity",
        compose_present,
        "compose files are present" if compose_present
        else f"{manifest.install_dir} does not look like a PatchTrack installation",
    ))

    state = inspect_runtime(manifest)
    checks.append(base.Check("container runtime", state.ready, state.detail))
    present = bool(state.containers or state.networks or state.volumes)
    if present and not compose_present:
        checks.append(base.Check(
            "compose project files",
            False,
            "the compose project still exists but its files are missing; "
            "restore them or remove the containers manually before uninstalling",
        ))
    checks.append(base.Check(
        "compose project",
        True,
        f"{len(state.containers)} container(s), {len(state.networks)} network(s), "
        f"{len(state.volumes)} volume(s)"
        if state.ready else "unknown until the runtime is available",
        required=False,
    ))

    if options.purge_data:
        checks.append(base.Check(
            "data volumes to delete",
            True,
            ", ".join(state.volumes) if state.volumes else "none found; nothing to purge",
            required=False,
            warning=bool(state.volumes),
        ))
    else:
        checks.append(base.Check(
            "data volumes",
            True,
            (", ".join(state.volumes) + " will be preserved")
            if state.volumes else "none found",
            required=False,
        ))

    checks.append(base.Check(
        "container image",
        True,
        manifest.image + (" exists" if state.image_present else " is not present")
        if not options.keep_image else manifest.image + " will be kept",
        required=False,
    ))

    env_ok = True
    env_detail = "not present"
    if manifest.env_file.is_symlink():
        try:
            target = manifest.env_file.resolve()
            target.relative_to(manifest.config_dir.resolve())
            env_detail = f"symlink to {target}"
        except (OSError, ValueError):
            env_ok = False
            env_detail = f"symlink points outside {manifest.config_dir}"
    elif manifest.env_file.is_file():
        env_detail = "regular file; will be removed"
    elif not manifest.env_file.exists():
        env_detail = "not present; continuing"
    checks.append(base.Check("environment file", env_ok, env_detail))

    top, secret = unknown_config_entries(manifest)
    leftovers = top + secret
    if leftovers:
        checks.append(base.Check(
            "configuration to preserve",
            True,
            f"{len(leftovers)} file(s) not created by the installer will be left in place",
            required=False,
            warning=True,
        ))
    else:
        checks.append(base.Check(
            "configuration to preserve", True, "none", required=False,
        ))

    user = infer_service_user(manifest)
    if options.keep_account:
        account_detail = "will be kept"
        account_ok = True
    elif not user:
        account_detail = "no service account recorded; none will be removed"
        account_ok = True
    elif user in PROTECTED_ACCOUNT_NAMES:
        account_ok = False
        account_detail = f"refusing to remove protected account {user}"
    elif not (manifest.created_user or manifest.created_group):
        account_detail = (
            f"{user} existed before installation and will be kept; remove it manually "
            "if it is no longer needed"
        )
        account_ok = True
    else:
        try:
            pwd.getpwnam(user)
            account_detail = (
                f"{user} exists and will be removed if no application or "
                "configuration files remain"
            )
        except KeyError:
            account_detail = f"{user} does not exist"
        account_ok = True
    checks.append(base.Check("service account", account_ok, account_detail))

    if PROXY_DROPIN.exists():
        ours = proxy_dropin_is_ours(PROXY_DROPIN)
        if options.remove_proxy_config:
            checks.append(base.Check(
                "Docker proxy configuration",
                ours,
                f"{PROXY_DROPIN} will be removed"
                if ours else f"{PROXY_DROPIN} was not created by the installer; remove it manually",
            ))
        else:
            checks.append(base.Check(
                "Docker proxy configuration",
                True,
                f"{PROXY_DROPIN} will be left in place; use --remove-proxy-config to remove it",
                required=False,
            ))
    return checks, state


# --------------------------------------------------------------- uninstallation

class Uninstaller:
    def __init__(self, manifest: Manifest, options: Options, state: RuntimeState):
        self.manifest = manifest
        self.options = options
        self.state = state
        self.failures: List[str] = []
        self.user = "" if options.keep_account else infer_service_user(manifest)
        self.account_removed = False
        self.preserved_volumes: List[str] = []
        self.leftovers: List[Path] = []

    def _compose(self, *args: str) -> subprocess.CompletedProcess:
        return base.run_command(
            compose_command(self.manifest) + [
                "-f", "docker-compose.yml",
                "-f", "docker-compose.secrets.yml",
                *args,
            ],
            cwd=self.manifest.install_dir,
            check=False,
            capture=True,
        )

    def _remove_volumes(self) -> None:
        binary = "docker" if self.manifest.runtime == "docker" else "podman"
        remaining = _project_items(self.manifest, "volumes") or []
        for name in remaining:
            result = base.run_command(
                [binary, "volume", "rm", name], check=False, capture=True
            )
            if result.returncode != 0:
                self.failures.append(
                    f"could not remove data volume {name}: "
                    + (result.stderr or result.stdout or "").strip()
                )

    def remove_containers(self) -> bool:
        base.step("Removing the PatchTrack containers and networks")
        self.preserved_volumes = [] if self.options.purge_data else list(self.state.volumes)
        project_present = bool(
            self.state.containers
            or self.state.networks
            or (self.options.purge_data and self.state.volumes)
        )
        if not project_present:
            base.info("no PatchTrack containers or networks are running")
        else:
            args = ["down", "--remove-orphans"]
            if self.options.purge_data:
                args.append("--volumes")
            result = self._compose(*args)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or "").strip()
                self.failures.append(
                    "compose down failed: "
                    + (detail.splitlines()[-1] if detail else "unknown error")
                )
                return False
            base.info("containers and networks removed")
        if self.options.purge_data:
            self._remove_volumes()
            self.preserved_volumes = []
        else:
            self.preserved_volumes = list(self.state.volumes)
        return True

    def remove_image(self) -> None:
        if self.options.keep_image:
            base.info(f"keeping image {self.manifest.image}")
            return
        base.step("Removing the PatchTrack image")
        if not base.image_exists(self.manifest.runtime, self.manifest.image):
            base.info(f"image {self.manifest.image} is not present")
            return
        binary = "docker" if self.manifest.runtime == "docker" else "podman"
        result = base.run_command(
            [binary, "image", "rm", "-f", self.manifest.image],
            check=False, capture=True,
        )
        if result.returncode != 0:
            self.failures.append(
                "could not remove image "
                + self.manifest.image
                + ": "
                + (result.stderr or result.stdout or "").strip()
            )
            return
        base.info(f"image {self.manifest.image} removed")

    def remove_proxy_config(self) -> None:
        if not self.options.remove_proxy_config or not PROXY_DROPIN.exists():
            return
        base.step("Removing the Docker proxy configuration")
        if not proxy_dropin_is_ours(PROXY_DROPIN):
            self.failures.append(
                f"{PROXY_DROPIN} was not created by the installer and was left in place"
            )
            return
        try:
            PROXY_DROPIN.unlink()
        except OSError as exc:
            self.failures.append(f"could not remove {PROXY_DROPIN}: {exc}")
            return
        reload_result = base.run_command(
            ["systemctl", "daemon-reload"], check=False, capture=True
        )
        if reload_result.returncode != 0:
            self.failures.append(
                "could not reload systemd: " + (reload_result.stderr or "").strip()
            )
            return
        active = base.run_command(
            ["systemctl", "is-active", "docker"], capture=True, check=False
        )
        if active.returncode == 0 and active.stdout.strip() == "active":
            restart = base.run_command(
                ["systemctl", "restart", "docker"], check=False, capture=True
            )
            if restart.returncode != 0:
                self.failures.append(
                    "could not restart Docker: " + (restart.stderr or "").strip()
                )
                return
        base.info("Docker proxy configuration removed")

    def remove_files(self) -> bool:
        base.step("Removing the application files")
        if self.manifest.install_dir.is_symlink():
            self.failures.append(
                f"{self.manifest.install_dir} became a symbolic link; refusing to remove it"
            )
            return False
        try:
            base.remove_tree(self.manifest.install_dir)
        except (OSError, base.InstallerError) as exc:
            self.failures.append(f"could not remove {self.manifest.install_dir}: {exc}")
            return False
        base.info(f"removed {self.manifest.install_dir}")

        base.step("Removing installer-managed configuration")
        try:
            removed, self.leftovers = cleanup_managed_config(self.manifest)
        except OSError as exc:
            self.failures.append(f"could not remove configuration files: {exc}")
            return False
        base.info(f"removed {len(removed)} configuration item(s)")
        if self.leftovers:
            base.warn(
                f"{len(self.leftovers)} file(s) not created by the installer were "
                "preserved in the configuration directory"
            )
        return True

    def remove_account(self) -> None:
        if not self.user or not (self.manifest.created_user or self.manifest.created_group):
            return
        base.step(f"Removing the service account {self.user}")
        if self.manifest.config_dir.exists() or self.manifest.install_dir.exists():
            base.warn(
                f"keeping account {self.user}: files remain in the application or "
                "configuration directory"
            )
            return
        if self.manifest.created_user:
            try:
                pwd.getpwnam(self.user)
            except KeyError:
                base.info(f"user {self.user} does not exist")
            else:
                result = base.run_command(["userdel", self.user], check=False, capture=True)
                if result.returncode != 0:
                    self.failures.append(
                        f"could not remove user {self.user}: "
                        + (result.stderr or result.stdout or "").strip()
                    )
                    return
                base.info(f"user {self.user} removed")
        if self.manifest.created_group:
            try:
                grp.getgrnam(self.service_group)
            except KeyError:
                self.account_removed = True
                return
            result = base.run_command(
                ["groupdel", self.service_group], check=False, capture=True
            )
            if result.returncode != 0:
                self.failures.append(
                    f"could not remove group {self.service_group}: "
                    + (result.stderr or result.stdout or "").strip()
                )
                return
            base.info(f"group {self.service_group} removed")
        self.account_removed = True

    @property
    def service_group(self) -> str:
        return self.manifest.service_group or self.user

    def run(self) -> bool:
        removed = self.remove_containers()
        self.remove_image()
        self.remove_proxy_config()
        if removed:
            files_removed = self.remove_files()
            if files_removed:
                self.remove_account()
        else:
            base.warn("leaving files and the service account in place because the "
                      "containers could not be removed")
        return not self.failures


# ------------------------------------------------------------------- postcheck

def collect_postflight(
    manifest: Manifest, options: Options, uninstaller: Uninstaller
) -> List[base.Check]:
    checks: List[base.Check] = []
    state = inspect_runtime(manifest)
    checks.append(base.Check("container runtime", state.ready, state.detail))

    checks.append(base.Check(
        "application directory removed",
        not manifest.install_dir.exists() and not manifest.install_dir.is_symlink(),
        f"{manifest.install_dir} still exists"
        if manifest.install_dir.exists() else str(manifest.install_dir),
    ))
    managed_present = [
        path for path in (
            manifest.env_file,
            manifest.path,
            *(manifest.secrets_dir / name for name in MANAGED_SECRETS),
        ) if path.exists() or path.is_symlink()
    ]
    checks.append(base.Check(
        "installer configuration removed",
        not managed_present,
        ", ".join(str(path) for path in managed_present)
        if managed_present else "installer-created files are gone",
    ))
    checks.append(base.Check(
        "containers and networks removed",
        not state.containers and not state.networks,
        f"{len(state.containers)} container(s), {len(state.networks)} network(s) remain"
        if state.containers or state.networks else "none remain",
    ))
    checks.append(base.Check(
        "data volumes purged" if options.purge_data else "data volumes preserved",
        not state.volumes if options.purge_data else True,
        (", ".join(state.volumes) if state.volumes else "none") if options.purge_data
        else (", ".join(uninstaller.preserved_volumes) if uninstaller.preserved_volumes
              else "none"),
    ))
    if not options.keep_image:
        checks.append(base.Check(
            "image removed",
            not state.image_present,
            manifest.image + (" still exists" if state.image_present else " is gone"),
        ))
    if uninstaller.account_removed:
        if manifest.created_user:
            try:
                pwd.getpwnam(uninstaller.user)
                user_gone = False
            except KeyError:
                user_gone = True
            checks.append(base.Check(
                "service user removed",
                user_gone,
                uninstaller.user + (" still exists" if not user_gone else " is gone"),
            ))
        if manifest.created_group:
            try:
                grp.getgrnam(uninstaller.service_group)
                group_gone = False
            except KeyError:
                group_gone = True
            checks.append(base.Check(
                "service group removed",
                group_gone,
                uninstaller.service_group
                + (" still exists" if not group_gone else " is gone"),
            ))
    if options.remove_proxy_config:
        checks.append(base.Check(
            "Docker proxy configuration removed",
            not PROXY_DROPIN.exists(),
            str(PROXY_DROPIN) + (" still exists" if PROXY_DROPIN.exists() else " is gone"),
        ))

    if manifest.config_dir.exists():
        checks.append(base.Check(
            "configuration directory",
            True,
            f"{manifest.config_dir} remains with operator-created files"
            if uninstaller.leftovers else f"{manifest.config_dir} remains",
            required=False,
        ))
    return checks


# ------------------------------------------------------------------------ flow

def summary(manifest: Manifest, options: Options, state: RuntimeState,
            leftovers: List[Path]) -> None:
    print()
    print(base.colour("Uninstallation summary", "1;1"))
    print(f"    Application directory : {manifest.install_dir}")
    print(f"    Configuration         : {manifest.config_dir}")
    print(f"    Runtime               : {manifest.runtime}")
    print(f"    Installation mode     : {manifest.installation_mode}")
    if manifest.airgap_bundle:
        print(f"    Air-gap bundle        : {manifest.airgap_bundle} (kept)")
    print(f"    Compose project       : {manifest.compose_project}")
    print(f"    Container image       : "
          f"{'keep ' if options.keep_image else 'remove '}{manifest.image}")
    user = infer_service_user(manifest)
    owned = manifest.created_user or manifest.created_group
    if options.keep_account or not user:
        account = "keep"
    elif not owned:
        account = f"keep {user} (pre-existing, not created by the installer)"
    else:
        account = f"remove {user}"
    print(f"    Service account       : {account}")
    print(f"    Data volumes          : "
          f"{'DELETE ' if options.purge_data else 'preserve '}"
          f"{len(state.volumes)} volume(s)")
    if state.volumes:
        print(f"      {', '.join(state.volumes)}")
    print(f"    Operator files        : "
          + (f"{len(leftovers)} preserved" if leftovers else "none"))
    print("    There is no rollback: verify the summary before continuing.")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="uninstall.sh",
        description=(
            "Guided PatchTrack uninstallation: prechecks the deployment, removes "
            "only what the installer created, and verifies the result."
        ),
    )
    parser.add_argument(
        "--check", action="store_true",
        help="run the prechecks and print the plan without changing anything",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="non-interactive; combine with --purge-data to delete data volumes",
    )
    parser.add_argument(
        "--config-dir", default=str(base.DEFAULT_CONFIG_DIR),
        help="configuration directory containing install.json (default: /etc/patchtrack)",
    )
    parser.add_argument(
        "--purge-data", action="store_true",
        help="ALSO delete the PostgreSQL, report, Solaris inbox, TLS and Grafana volumes",
    )
    parser.add_argument(
        "--keep-image", action="store_true",
        help="leave the PatchTrack container image in place",
    )
    parser.add_argument(
        "--keep-account", action="store_true",
        help="leave the service user and group in place",
    )
    parser.add_argument(
        "--remove-proxy-config", action="store_true",
        help="remove the installer-owned Docker proxy systemd drop-in and restart Docker",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    options = Options(
        check=args.check,
        yes=args.yes,
        purge_data=args.purge_data,
        keep_image=args.keep_image,
        keep_account=args.keep_account,
        remove_proxy_config=args.remove_proxy_config,
        config_dir=base.clean_path(args.config_dir),
    )
    manifest_path = options.config_dir / "install.json"

    try:
        manifest = load_manifest(manifest_path)
    except UninstallError as exc:
        base.error(str(exc))
        return 1

    if not manifest.service_user:
        manifest.service_user = infer_service_user(manifest)
        manifest.service_group = manifest.service_user

    checks, state = collect_preflight(manifest, options)
    print(base.colour("PatchTrack uninstall prechecks", "1;1"))
    failures, warnings = base.print_checks(checks)
    if failures:
        print()
        base.error(f"{failures} required precheck(s) failed; nothing has been changed.")
        return 1

    top, secret = unknown_config_entries(manifest)
    leftovers = top + secret
    summary(manifest, options, state, leftovers)

    if options.check:
        print()
        if warnings:
            base.warn(f"{warnings} warning(s); review the notes above.")
        else:
            base.info("All required prechecks passed; nothing has been changed.")
        return 0

    if options.purge_data and state.volumes:
        if not options.yes:
            base.warn(
                "This permanently deletes the database, reports, Solaris inbox, "
                "TLS certificate and Grafana data volumes listed above."
            )
            try:
                confirmed = _ask_yes_no(
                    "Delete ALL PatchTrack data volumes permanently?", False
                )
            except UserCancelled as exc:
                base.error(str(exc))
                return 2
            if not confirmed:
                base.info("Uninstallation cancelled; nothing has been changed.")
                return 0

    if not options.yes:
        try:
            if not _ask_yes_no("Proceed with the uninstallation?", False):
                base.info("Uninstallation cancelled; nothing has been changed.")
                return 0
        except UserCancelled as exc:
            base.error(str(exc))
            return 2

    uninstaller = Uninstaller(manifest, options, state)
    success = uninstaller.run()

    print()
    print(base.colour("Post-uninstallation checks", "1;1"))
    post = collect_postflight(manifest, options, uninstaller)
    post_failures, _ = base.print_checks(post)

    if uninstaller.leftovers:
        print()
        base.warn("Preserved operator-created files:")
        for path in uninstaller.leftovers:
            print(f"    - {path}")
    if uninstaller.preserved_volumes:
        print()
        base.warn("Preserved data volumes (back them up or remove them deliberately):")
        for name in uninstaller.preserved_volumes:
            print(f"    - {name}")
    if PROXY_DROPIN.exists() and not options.remove_proxy_config:
        print()
        base.warn(
            f"The Docker proxy drop-in remains at {PROXY_DROPIN}; "
            "use --remove-proxy-config if it is no longer needed."
        )

    print()
    if uninstaller.failures:
        base.error("Uninstallation completed with errors:")
        for failure in uninstaller.failures:
            print(f"    - {failure}", file=sys.stderr)
        return 1
    if post_failures:
        base.error(f"{post_failures} post-uninstallation check(s) failed.")
        return 1
    base.info("PatchTrack has been uninstalled; the checks above passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
