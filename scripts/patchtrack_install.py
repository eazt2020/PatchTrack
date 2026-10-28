#!/usr/bin/env python3
"""Guided PatchTrack installer for RHEL 9 / Rocky 9.

The installer is deliberately a thin wrapper around the documented deployment:

1. it checks the host and every dependency it needs *before* changing anything;
2. it asks for the few parameters a deployment must choose;
3. it installs the container runtime if asked, creates a service account,
   installs the application, generates every bootstrap secret and starts the
   stack;
4. it verifies the result end to end (HTTPS, every service, migrations and the
   first administrator);
5. if any step fails, it reverses exactly the actions it recorded, newest
   first. A target directory that already holds a PatchTrack deployment is
   refused rather than overwritten, and a rollback never removes a directory
   that existed before the installer ran.

Nothing in this file prints a generated secret. The administrator password is
read without echo, used once for the bootstrap request and never written to
disk or to the command line.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import getpass
import grp
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import signal
import socket
import ssl
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VERSION = "3.0.0"
DEFAULT_INSTALL_DIR = Path("/opt/patchtrack")
DEFAULT_CONFIG_DIR = Path("/etc/patchtrack")
DEFAULT_SERVICE_USER = "patchtrack"
DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 8005
MIN_PASSWORD = 12
MAX_PASSWORD = 512

# Directories the installer must never remove as a whole, even if a malformed
# rollback record somehow names one of them.
PROTECTED_PATHS = {
    Path("/"), Path("/bin"), Path("/boot"), Path("/dev"), Path("/etc"),
    Path("/home"), Path("/lib"), Path("/lib64"), Path("/media"), Path("/mnt"),
    Path("/opt"), Path("/proc"), Path("/root"), Path("/run"), Path("/sbin"),
    Path("/srv"), Path("/sys"), Path("/tmp"), Path("/usr"), Path("/var"),
    Path("/var/lib"), Path("/var/log"), Path("/var/tmp"),
}


class InstallerError(RuntimeError):
    """A failure that should stop the installation and roll it back."""


class UserCancelled(InstallerError):
    """The operator chose not to continue."""


# --------------------------------------------------------------------------- UI

_STEP = 0


def _tty() -> bool:
    return sys.stdout.isatty()


def colour(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _tty() else text


def step(title: str) -> None:
    global _STEP
    _STEP += 1
    print()
    print(colour(f"[{_STEP}] {title}", "1;36"))


def info(message: str) -> None:
    print(f"    {message}")


def warn(message: str) -> None:
    print(colour(f"  ! {message}", "1;33"))


def error(message: str) -> None:
    print(colour(f"  x {message}", "1;31"), file=sys.stderr)


def run_command(
    args: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    capture: bool = False,
    check: bool = True,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    """Run a command without a shell and with a predictable failure mode."""
    printable = shlex.join([str(a) for a in args])
    try:
        result = subprocess.run(
            [str(a) for a in args],
            cwd=str(cwd) if cwd else None,
            text=True,
            capture_output=capture,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise InstallerError(f"required command not found: {args[0]}") from exc
    if check and result.returncode != 0:
        detail = ""
        if capture:
            detail = (result.stderr or result.stdout or "").strip()
            if detail:
                detail = f": {detail.splitlines()[-1][:300]}"
        raise InstallerError(f"command failed ({result.returncode}): {printable}{detail}")
    return result


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def command_version(args: Sequence[str], pattern: str) -> Optional[str]:
    try:
        result = run_command(args, capture=True, check=False)
    except InstallerError:
        return None
    if result.returncode != 0:
        return None
    match = re.search(pattern, f"{result.stdout} {result.stderr}")
    return match.group(1) if match else None


# ----------------------------------------------------------------------- inputs

def _path_error(value: str) -> Optional[str]:
    if not value.startswith("/"):
        return "must be an absolute path"
    if value != "/" and value.endswith("/"):
        value = value.rstrip("/")
    if any(part in ("", ".", "..") for part in value.split("/")[1:]):
        return "must not contain empty, '.' or '..' path segments"
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", value):
        return "may contain only letters, digits, '.', '_', '-' and '/'"
    path = Path(value)
    if path in PROTECTED_PATHS or len(path.parts) < 2:
        return "must be a dedicated application directory, not a system directory"
    return None


def clean_path(value: str) -> Path:
    return Path(value.rstrip("/") if value != "/" else value)


def validate_install_path(value: str, label: str = "path") -> Optional[str]:
    problem = _path_error(value)
    if problem:
        return f"{label} {problem}"
    path = clean_path(value).resolve()
    source = REPO_ROOT.resolve()
    if path == source:
        return "must not be the source directory the installer is running from"
    try:
        path.relative_to(source)
        return "must not be inside the source directory"
    except ValueError:
        pass
    try:
        source.relative_to(path)
        return "must not contain the source directory"
    except ValueError:
        pass
    return None


def paths_overlap(first: Path, second: Path) -> bool:
    first = first.resolve()
    second = second.resolve()
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def validate_bind(value: str) -> Optional[str]:
    import ipaddress

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "must be an IPv4 address such as 0.0.0.0 or 192.0.2.10"
    if address.version != 4:
        return "only IPv4 bind addresses are supported"
    if address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255"):
        return "must be a unicast address"
    return None


def validate_port(value: str) -> Optional[str]:
    try:
        port = int(value, 10)
    except ValueError:
        return "must be a whole number"
    if not 1 <= port <= 65535:
        return "must be between 1 and 65535"
    return None


def validate_hostname(value: str) -> Optional[str]:
    if len(value) > 253:
        return "is too long"
    labels = value.split(".")
    if any(not label or len(label) > 63 for label in labels):
        return "is not a valid DNS name or IP address"
    for label in labels:
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label):
            return "is not a valid DNS name or IP address"
    return None


def validate_service_user(value: str) -> Optional[str]:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value):
        return "must start with a lowercase letter and use only lowercase letters, digits, '_' or '-'"
    return None


def validate_admin_username(value: str) -> Optional[str]:
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,150}", value):
        return "must be 3-150 characters: letters, digits, '.', '_' or '-'"
    return None


def validate_email(value: str) -> Optional[str]:
    if value and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return "must be a valid email address, or left empty"
    return None


def _ask(label: str, default: str, validator: Callable[[str], Optional[str]]) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        try:
            raw = input(f"    {label}{suffix}: ").strip()
        except EOFError as exc:
            raise UserCancelled("no input available; rerun interactively or use --yes") from exc
        value = raw or default
        problem = validator(value)
        if problem:
            warn(f"{label} {problem}")
            continue
        return value


def _ask_yes_no(label: str, default: bool = True) -> bool:
    marker = "Y/n" if default else "y/N"
    while True:
        try:
            raw = input(f"    {label} [{marker}]: ").strip().lower()
        except EOFError as exc:
            raise UserCancelled("no input available; rerun interactively or use --yes") from exc
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        warn("please answer yes or no")


def _ask_password() -> str:
    while True:
        try:
            first = getpass.getpass("    Administrator password (minimum 12 characters): ")
            if not MIN_PASSWORD <= len(first) <= MAX_PASSWORD:
                warn(f"password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters")
                continue
            second = getpass.getpass("    Repeat administrator password: ")
        except EOFError as exc:
            raise UserCancelled("no input available; rerun interactively or use --yes") from exc
        if first != second:
            warn("passwords do not match")
            continue
        return first


def detected_ip() -> Optional[str]:
    """The address a default route would use. No packet is sent."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


def default_hostname() -> str:
    name = socket.getfqdn().strip()
    if name and validate_hostname(name) is None:
        return name
    return detected_ip() or "localhost"


@dataclass
class Config:
    install_dir: Path = DEFAULT_INSTALL_DIR
    config_dir: Path = DEFAULT_CONFIG_DIR
    service_user: str = DEFAULT_SERVICE_USER
    bind_address: str = DEFAULT_BIND
    gateway_port: int = DEFAULT_PORT
    tls_hostname: str = field(default_factory=default_hostname)
    admin_username: str = "admin"
    admin_display_name: str = "PatchTrack Administrator"
    admin_email: str = ""
    admin_password: str = field(default="", repr=False)
    create_admin: bool = True
    runtime: str = "auto"
    min_disk_gb: int = 20
    min_memory_gb: int = 4
    min_cpu: int = 2
    allow_permissive: bool = False
    allow_unsupported_os: bool = False

    @property
    def secrets_dir(self) -> Path:
        return self.config_dir / "secrets"

    @property
    def env_file(self) -> Path:
        return self.config_dir / ".env"

    @property
    def version(self) -> str:
        return os.environ.get("PATCHTRACK_VERSION", DEFAULT_VERSION)

    @property
    def image(self) -> str:
        return f"patchtrack:{self.version}"

    @property
    def probe_host(self) -> str:
        return "127.0.0.1" if self.bind_address == "0.0.0.0" else self.bind_address

    @property
    def base_url(self) -> str:
        return f"https://{self.probe_host}:{self.gateway_port}"

    @property
    def public_url(self) -> str:
        host = self.tls_hostname or self.bind_address
        if host == "0.0.0.0":
            host = detected_ip() or "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"https://{host}:{self.gateway_port}"


# ------------------------------------------------------------------ preflight

@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True
    warning: bool = False


@dataclass
class RuntimeInfo:
    docker_bin: bool = False
    docker_compose: bool = False
    docker_version: Optional[str] = None
    compose_version: Optional[str] = None
    podman_bin: bool = False
    podman_compose: bool = False
    podman_version: Optional[str] = None
    daemon_active: bool = False

    @property
    def docker_ready(self) -> bool:
        return self.docker_bin and self.docker_compose

    @property
    def podman_ready(self) -> bool:
        return self.podman_bin and self.podman_compose


@dataclass
class RuntimePlan:
    runtime: str
    install_packages: bool
    packages: List[str] = field(default_factory=list)
    start_docker: bool = False

    @property
    def label(self) -> str:
        base = "Docker Engine with Compose v2" if self.runtime == "docker" else "Podman with podman-compose"
        return f"{base} (will be installed)" if self.install_packages else base


def detect_runtime() -> RuntimeInfo:
    info = RuntimeInfo()
    info.docker_bin = command_exists("docker")
    if info.docker_bin:
        info.docker_version = command_version(["docker", "--version"], r"version\s+([0-9][0-9.]*)")
        info.docker_compose = command_version(
            ["docker", "compose", "version"], r"v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)"
        ) is not None
        info.compose_version = command_version(
            ["docker", "compose", "version"], r"v?([0-9]+\.[0-9]+(?:\.[0-9]+)?)"
        )
        if command_exists("systemctl"):
            active = run_command(["systemctl", "is-active", "docker"], capture=True, check=False)
            info.daemon_active = active.returncode == 0 and active.stdout.strip() == "active"
    info.podman_bin = command_exists("podman")
    if info.podman_bin:
        info.podman_version = command_version(["podman", "--version"], r"version\s+([0-9][0-9.]*)")
        info.podman_compose = command_exists("podman-compose")
    return info


def os_release() -> dict:
    values = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def selinux_enforcing() -> Optional[bool]:
    path = Path("/sys/fs/selinux/enforce")
    try:
        return path.read_text().strip() == "1"
    except OSError:
        return None


def memory_bytes() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def nearest_existing(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(str(nearest_existing(path))).free


def port_available(bind: str, port: int) -> Tuple[bool, str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((bind, port))
        return True, "free"
    except OSError as exc:
        return False, str(exc)
    finally:
        sock.close()


def directory_state(path: Path) -> Tuple[bool, bool]:
    """Return (exists, is a directory containing entries)."""
    if not path.exists():
        return False, False
    if not path.is_dir():
        return True, False
    try:
        next(path.iterdir())
        nonempty = True
    except StopIteration:
        nonempty = False
    return True, nonempty


def target_path_checks(cfg: Config) -> List[Check]:
    checks = []
    for label, path in (
        ("install directory", cfg.install_dir),
        ("configuration directory", cfg.config_dir),
        ("secrets directory", cfg.secrets_dir),
    ):
        exists, nonempty = directory_state(path)
        if path.is_symlink():
            checks.append(Check(label, False, f"{path} is a symbolic link; use a real directory"))
        elif exists and not path.is_dir():
            checks.append(Check(label, False, f"{path} is not a directory"))
        elif exists and nonempty:
            checks.append(Check(label, False, f"{path} is not empty; refusing to overwrite it"))
        else:
            checks.append(Check(label, True, f"{path} is available"))
    problem = validate_install_path(str(cfg.install_dir), "install directory")
    if problem:
        checks.append(Check("install path", False, problem))
    problem = validate_install_path(str(cfg.config_dir), "configuration directory")
    if problem:
        checks.append(Check("configuration path", False, problem))
    if cfg.config_dir == cfg.install_dir:
        checks.append(Check("path separation", False, "configuration and install directories must differ"))
    elif paths_overlap(cfg.install_dir, cfg.config_dir):
        checks.append(Check(
            "path separation", False,
            "configuration and install directories must not contain each other",
        ))
    return checks


def compose_project_present(runtime: str) -> Tuple[bool, str]:
    """Check for containers or volumes already owned by the compose project."""
    try:
        if runtime == "docker":
            containers = run_command(
                ["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=patchtrack",
                 "--format", "{{.ID}}"], capture=True, check=True
            ).stdout.strip()
            volumes = run_command(
                ["docker", "volume", "ls", "--filter", "label=com.docker.compose.project=patchtrack",
                 "--format", "{{.Name}}"], capture=True, check=True
            ).stdout.strip()
        else:
            containers = run_command(
                ["podman", "ps", "-a", "--filter", "label=com.docker.compose.project=patchtrack",
                 "--format", "{{.ID}}"], capture=True, check=True
            ).stdout.strip()
            volumes = run_command(
                ["podman", "volume", "ls", "--filter", "label=com.docker.compose.project=patchtrack",
                 "--format", "{{.Name}}"], capture=True, check=True
            ).stdout.strip()
        if containers or volumes:
            return True, "existing PatchTrack compose containers or volumes were found"
    except InstallerError:
        return False, "could not inspect the container runtime"
    return False, "no existing PatchTrack compose project"


def network_reachable(url: str) -> bool:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "PatchTrack-installer"})
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read(1)
            return True
    except urllib.error.HTTPError as exc:
        # Registry endpoints commonly answer 401/403 to an unauthenticated
        # request. That still proves the endpoint is reachable.
        return exc.code in (401, 403)
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def collect_preflight(cfg: Config, runtime: RuntimeInfo, plan: RuntimePlan) -> List[Check]:
    checks: List[Check] = []

    checks.append(Check(
        "root privileges",
        os.geteuid() == 0,
        "running as root" if os.geteuid() == 0 else "rerun with sudo",
    ))

    release = os_release()
    os_id = release.get("ID", "unknown")
    version_id = release.get("VERSION_ID", "unknown")
    supported = os_id in ("rhel", "rocky") and version_id.split(".")[0] == "9"
    if cfg.allow_unsupported_os:
        supported = True
    checks.append(Check(
        "operating system",
        supported,
        f"{release.get('PRETTY_NAME', os_id)} ({version_id})"
        + ("" if supported else " — this installer targets RHEL 9 / Rocky 9"),
    ))

    machine = os.uname().machine
    checks.append(Check(
        "architecture",
        machine in ("x86_64", "aarch64") or cfg.allow_unsupported_os,
        machine,
    ))

    cores = os.cpu_count() or 1
    checks.append(Check(
        "CPU", cores >= cfg.min_cpu, f"{cores} vCPU (minimum {cfg.min_cpu})"
    ))

    memory = memory_bytes()
    checks.append(Check(
        "memory",
        memory >= cfg.min_memory_gb * 1024 ** 3,
        f"{memory / 1024 ** 3:.1f} GiB (minimum {cfg.min_memory_gb} GiB)",
    ))

    enforcing = selinux_enforcing()
    if enforcing is None:
        selinux_ok = cfg.allow_unsupported_os
        selinux_detail = "SELinux status could not be read"
    elif enforcing:
        selinux_ok = True
        selinux_detail = "enforcing"
    else:
        selinux_ok = cfg.allow_permissive
        selinux_detail = "not enforcing; use --allow-permissive only for a documented exception"
    checks.append(Check("SELinux", selinux_ok, selinux_detail))

    disk_gb = cfg.min_disk_gb * 1024 ** 3
    for label, path in (
        ("disk for application", cfg.install_dir),
        ("disk for configuration", cfg.config_dir),
        ("disk for container data", Path("/var/lib")),
    ):
        available = free_bytes(path)
        checks.append(Check(
            label,
            available >= disk_gb,
            f"{available / 1024 ** 3:.1f} GiB free on the filesystem for {path} "
            f"(minimum {cfg.min_disk_gb} GiB)",
        ))

    required_commands = ["useradd", "groupadd", "systemctl"]
    if plan.install_packages:
        required_commands.append("dnf")
    if plan.runtime == "docker":
        required_commands.append("systemctl")
    for command in sorted(set(required_commands)):
        checks.append(Check(
            f"command {command}", command_exists(command),
            "found" if command_exists(command) else f"{command} is required",
        ))

    if plan.install_packages:
        checks.append(Check(
            "package manager",
            command_exists("dnf"),
            "dnf is available" if command_exists("dnf") else "dnf is required to install the runtime",
        ))

    ready = runtime.docker_ready if plan.runtime == "docker" else runtime.podman_ready
    if plan.install_packages:
        runtime_detail = f"{plan.label}; dnf will install it"
        runtime_ok = command_exists("dnf")
    elif ready:
        version = runtime.docker_version if plan.runtime == "docker" else runtime.podman_version
        runtime_detail = f"{plan.label} {version or ''}".strip()
        runtime_ok = True
    else:
        runtime_detail = f"{plan.label} is incomplete"
        runtime_ok = False
    checks.append(Check("container runtime", runtime_ok, runtime_detail))

    free, detail = port_available(cfg.bind_address, cfg.gateway_port)
    checks.append(Check(
        "gateway port",
        free,
        f"{cfg.bind_address}:{cfg.gateway_port} is {detail}",
    ))

    checks.extend(target_path_checks(cfg))

    if ready:
        present, detail = compose_project_present(plan.runtime)
        checks.append(Check(
            "existing compose project", not present, detail,
        ))

    network_checks = [
        ("container image registry", "https://registry-1.docker.io/v2/"),
        ("base image registry", "https://registry.access.redhat.com/v2/"),
        ("Python package index", "https://pypi.org/simple/"),
    ]
    if plan.install_packages:
        network_checks.append(("Docker package repository", "https://download.docker.com/linux/"))
    for label, url in network_checks:
        reachable = network_reachable(url)
        checks.append(Check(
            label,
            True,
            "reachable" if reachable else
            "not reachable right now; the build may fail unless images and packages are mirrored or cached",
            required=False,
            warning=not reachable,
        ))
    return checks


def print_checks(checks: Iterable[Check]) -> Tuple[int, int]:
    failures = 0
    warnings = 0
    for check in checks:
        if not check.ok:
            failures += 1
            tag = colour("FAIL", "1;31")
        elif check.warning:
            warnings += 1
            tag = colour("WARN", "1;33")
        else:
            tag = colour(" OK ", "1;32")
        print(f"    [{tag}] {check.name}: {check.detail}")
    return failures, warnings


# --------------------------------------------------------------------- actions

class RollbackAction:
    description = "change"

    def rollback(self) -> None:
        raise NotImplementedError


class CreatedDirectory(RollbackAction):
    def __init__(self, path: Path, description: str = "created directory"):
        self.path = path
        self.description = f"{description}: {path}"

    def rollback(self) -> None:
        remove_tree(self.path)


class EmptyOnRollback(RollbackAction):
    """A directory that existed but was empty before the installer used it."""

    def __init__(self, path: Path):
        self.path = path
        self.description = f"emptied pre-existing directory: {path}"

    def rollback(self) -> None:
        if not self.path.exists():
            return
        for child in self.path.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()


class DirectoryMetadata(RollbackAction):
    """Restore mode and ownership of a directory that already existed."""

    def __init__(self, path: Path, mode: int, uid: int, gid: int):
        self.path = path
        self.mode = mode
        self.uid = uid
        self.gid = gid
        self.description = f"restored metadata on pre-existing directory: {path}"

    def rollback(self) -> None:
        if not self.path.exists():
            return
        os.chmod(self.path, self.mode)
        os.chown(self.path, self.uid, self.gid)


class CreatedPath(RollbackAction):
    def __init__(self, path: Path, description: str = "created file"):
        self.path = path
        self.description = f"{description}: {path}"

    def rollback(self) -> None:
        try:
            if self.path.is_symlink() or self.path.is_file():
                self.path.unlink()
            elif self.path.exists():
                remove_tree(self.path)
        except FileNotFoundError:
            return


class RemoveTree(RollbackAction):
    def __init__(self, path: Path, description: str):
        self.path = path
        self.description = f"{description}: {path}"

    def rollback(self) -> None:
        remove_tree(self.path)


class CreatedAccount(RollbackAction):
    def __init__(self, kind: str, name: str):
        self.kind = kind
        self.name = name
        self.description = f"created {kind}: {name}"

    def rollback(self) -> None:
        if self.kind == "group":
            try:
                grp.getgrnam(self.name)
            except KeyError:
                return
            result = run_command(["groupdel", self.name], check=False, capture=True)
            if result.returncode != 0:
                raise InstallerError(
                    f"could not remove group {self.name}: {(result.stderr or '').strip()}"
                )
        else:
            result = run_command(["userdel", self.name], check=False, capture=True)
            if result.returncode != 0 and "does not exist" not in (result.stderr or ""):
                raise InstallerError(
                    f"could not remove user {self.name}: {(result.stderr or '').strip()}"
                )


class DockerServiceState(RollbackAction):
    def __init__(self, active: bool, enabled: bool):
        self.active = active
        self.enabled = enabled
        self.description = (
            f"docker service state (was active={active}, enabled={enabled})"
        )

    def _state(self, verb: str) -> bool:
        result = run_command(["systemctl", verb, "docker"], capture=True, check=False)
        return result.returncode == 0 and result.stdout.strip() in ("active", "enabled")

    def rollback(self) -> None:
        if not command_exists("systemctl"):
            raise InstallerError("systemctl is not available; restore the Docker service state manually")
        now_active = self._state("is-active")
        now_enabled = self._state("is-enabled")
        if now_active and not self.active:
            result = run_command(["systemctl", "stop", "docker"], check=False, capture=True)
            if result.returncode != 0:
                raise InstallerError(
                    "could not stop Docker: " + (result.stderr or "").strip()
                )
        if now_enabled and not self.enabled:
            result = run_command(["systemctl", "disable", "docker"], check=False, capture=True)
            if result.returncode != 0:
                raise InstallerError(
                    "could not disable Docker: " + (result.stderr or "").strip()
                )


class DnfTransaction(RollbackAction):
    def __init__(self, transaction_id: str, packages: Iterable[str]):
        self.transaction_id = transaction_id
        self.packages = sorted(set(packages))
        self.description = f"dnf transaction {transaction_id}"

    def rollback(self) -> None:
        if not command_exists("dnf"):
            raise InstallerError(
                f"dnf is unavailable; undo transaction {self.transaction_id} manually"
            )
        result = run_command(
            ["dnf", "-y", "history", "undo", self.transaction_id],
            check=False, capture=True,
        )
        if result.returncode == 0:
            return
        if self.packages:
            warn(
                f"dnf history undo {self.transaction_id} failed; removing the "
                f"{len(self.packages)} package(s) it added"
            )
            removed = run_command(
                ["dnf", "-y", "remove"] + self.packages,
                check=False, capture=True,
            )
            if removed.returncode == 0:
                return
            raise InstallerError(
                f"could not undo dnf transaction {self.transaction_id}: "
                + (removed.stderr or "").strip()
            )
        raise InstallerError(
            f"could not undo dnf transaction {self.transaction_id}: "
            + (result.stderr or "").strip()
        )


class CreatedRepoFile(RollbackAction):
    def __init__(self, path: Path):
        self.path = path
        self.description = f"created package repository file: {path}"

    def rollback(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            return


class ComposeProject(RollbackAction):
    def __init__(self, cfg: Config, runtime: str):
        self.cfg = cfg
        self.runtime = runtime
        self.description = "compose containers, networks and named volumes"

    def rollback(self) -> None:
        binary = "docker" if self.runtime == "docker" else "podman-compose"
        if not command_exists(binary):
            raise InstallerError(
                f"{binary} is unavailable; remove the PatchTrack compose project manually"
            )
        result = compose(
            self.cfg, self.runtime, "down", "--volumes", "--remove-orphans",
            check=False, capture=True,
        )
        if result.returncode != 0:
            raise InstallerError(
                "compose down failed: " + (result.stderr or result.stdout or "").strip()
            )


class ContainerImage(RollbackAction):
    def __init__(self, runtime: str, image: str):
        self.runtime = runtime
        self.image = image
        self.description = f"built container image: {image}"

    def rollback(self) -> None:
        if not image_exists(self.runtime, self.image):
            return
        if self.runtime == "docker":
            result = run_command(
                ["docker", "image", "rm", "-f", self.image], check=False, capture=True
            )
        else:
            result = run_command(
                ["podman", "image", "rm", "-f", self.image], check=False, capture=True
            )
        if result.returncode != 0:
            raise InstallerError(
                f"could not remove image {self.image}: "
                + (result.stderr or result.stdout or "").strip()
            )


# -------------------------------------------------------------------- helpers

def remove_tree(path: Path) -> None:
    path = path.resolve()
    if path in PROTECTED_PATHS:
        raise InstallerError(f"refusing to remove protected path {path}")
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    shutil.rmtree(str(path))


def ensure_group(name: str, actions: List[RollbackAction]) -> int:
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        pass
    if not command_exists("groupadd"):
        raise InstallerError("groupadd is required to create the service group")
    run_command(["groupadd", "--system", name])
    actions.append(CreatedAccount("group", name))
    return grp.getgrnam(name).gr_gid


def ensure_user(name: str, group: str, home: Path, actions: List[RollbackAction]) -> Tuple[int, int]:
    try:
        record = pwd.getpwnam(name)
        return record.pw_uid, record.pw_gid
    except KeyError:
        pass
    nologin = "/sbin/nologin" if Path("/sbin/nologin").exists() else "/usr/sbin/nologin"
    run_command([
        "useradd", "--system", "--gid", group, "--home-dir", str(home),
        "--shell", nologin, name,
    ])
    actions.append(CreatedAccount("user", name))
    record = pwd.getpwnam(name)
    return record.pw_uid, record.pw_gid


def prepare_directory(
    path: Path,
    actions: List[RollbackAction],
    *,
    mode: int,
    gid: Optional[int],
    label: str,
) -> None:
    """Create a directory, or take an empty pre-existing one, and record it."""
    if path.is_symlink() or path.exists():
        if path.is_symlink() or not path.is_dir():
            raise InstallerError(f"{label} {path} is not a real directory")
        if any(path.iterdir()):
            raise InstallerError(f"{label} {path} is not empty; refusing to overwrite it")
        current = path.stat()
        actions.append(DirectoryMetadata(
            path, stat.S_IMODE(current.st_mode), current.st_uid, current.st_gid
        ))
        actions.append(EmptyOnRollback(path))
        os.chmod(path, mode)
        if gid is not None:
            os.chown(path, 0, gid)
        return

    missing: List[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for candidate in reversed(missing):
        candidate.mkdir(mode=0o700)
        actions.append(CreatedDirectory(candidate, f"created {label}"))
    os.chmod(path, mode)
    if gid is not None:
        os.chown(path, 0, gid)


def write_file(
    path: Path,
    content: str,
    actions: List[RollbackAction],
    *,
    mode: int,
    gid: Optional[int] = None,
    description: str = "created file",
) -> None:
    if path.exists() or path.is_symlink():
        raise InstallerError(f"refusing to overwrite existing file {path}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(str(path), flags, mode)
    actions.append(CreatedPath(path, description))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(path, mode)
        if gid is not None:
            os.chown(path, 0, gid)
    finally:
        pass


def write_secret(path: Path, value: str, actions: List[RollbackAction], gid: int) -> None:
    write_file(path, value + "\n", actions, mode=0o640, gid=gid, description="created secret file")


def latest_dnf_transaction() -> Optional[str]:
    if not command_exists("dnf"):
        return None
    result = run_command(["dnf", "-q", "history", "list"], capture=True, check=False)
    if result.returncode != 0:
        return None
    ids = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s*\|", line)
        if match:
            ids.append(match.group(1))
    return max(ids, key=lambda value: int(value)) if ids else None


def rpm_packages() -> set:
    if not command_exists("rpm"):
        return set()
    result = run_command(["rpm", "-qa", "--qf", "%{NAME}\n"], capture=True, check=False)
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def dnf_install(packages: Sequence[str], actions: List[RollbackAction]) -> None:
    before_txn = latest_dnf_transaction()
    before_packages = rpm_packages()
    try:
        run_command(["dnf", "-y", "install"] + list(packages))
    finally:
        after_txn = latest_dnf_transaction()
        added = rpm_packages() - before_packages
        if after_txn and after_txn != before_txn:
            actions.append(DnfTransaction(after_txn, added))


def compose(
    cfg: Config,
    runtime: str,
    *args: str,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    if runtime == "docker":
        command = ["docker", "compose"]
    else:
        command = ["podman-compose"]
    command += [
        "-f", "docker-compose.yml",
        "-f", "docker-compose.secrets.yml",
    ] + list(args)
    return run_command(command, cwd=cfg.install_dir, check=check, capture=capture)


def image_exists(runtime: str, image: str) -> bool:
    command = ["docker", "image", "inspect", image] if runtime == "docker" else [
        "podman", "image", "exists", image,
    ]
    return run_command(command, check=False, capture=True).returncode == 0


# --------------------------------------------------------------- installation

ENV_TEMPLATE = """\
# Generated by the PatchTrack guided installer on {date}.
#
# Bootstrap credentials (database password, JWT secret, service token and the
# PostgreSQL password) are file-backed in {secrets_dir} and are intentionally
# blank here. Compose is always invoked with docker-compose.secrets.yml, which
# mounts those files. Keep this file mode 0640 and owned by root:{group}.

PATCHTRACK_VERSION={version}
PATCHTRACK_ENVIRONMENT=production
PATCHTRACK_SESSION_COOKIE_SECURE=auto
PATCHTRACK_LOG_LEVEL=info

POSTGRES_DB=patchtrack
POSTGRES_USER=patchtrack
POSTGRES_PASSWORD=
POSTGRES_IMAGE=docker.io/library/postgres:17

PATCHTRACK_DATABASE_URL=
PATCHTRACK_JWT_SECRET=
PATCHTRACK_SERVICE_TOKEN=
PATCHTRACK_SECRETS_KEY=

PATCHTRACK_BIND_ADDRESS={bind}
PATCHTRACK_GATEWAY_PORT={port}
PATCHTRACK_TLS_ENABLED=true
PATCHTRACK_TLS_HOSTNAME={hostname}
PATCHTRACK_TLS_DIR=/var/lib/patchtrack/tls
PATCHTRACK_REPORT_DIR=/var/lib/patchtrack/reports
PATCHTRACK_SOLARIS_INBOX=/var/lib/patchtrack/solaris-inbox

PATCHTRACK_FORWARDED_IPS=127.0.0.1
PATCHTRACK_INTERNAL_FORWARDED_IPS=172.16.0.0/12

# Grafana is optional and is not started by the installer. Set these and create
# the read-only role before enabling the dashboards profile.
PATCHTRACK_GF_DB_HOST=postgres
PATCHTRACK_GF_DB_PORT=5432
PATCHTRACK_GF_DB_NAME=patchtrack
PATCHTRACK_GF_DB_USER=patchtrack_grafana
PATCHTRACK_GF_DB_PASSWORD=
PATCHTRACK_GF_DB_SSLMODE=verify-full
GF_ADMIN_PASSWORD=
GF_SERVER_ROOT_URL=http://127.0.0.1:3000
"""


def build_env(cfg: Config, group: str) -> str:
    return ENV_TEMPLATE.format(
        date=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        secrets_dir=cfg.secrets_dir,
        group=group,
        version=cfg.version,
        bind=cfg.bind_address,
        port=cfg.gateway_port,
        hostname=cfg.tls_hostname,
    )


def copy_application(cfg: Config) -> None:
    def ignore(directory: str, names: List[str]) -> set:
        ignored = set()
        for name in names:
            if name in {
                ".git", ".runtime", "dist", "__pycache__", ".pytest_cache",
                ".mypy_cache", ".ruff_cache", "venv", ".venv",
            }:
                ignored.add(name)
            elif name == ".env":
                ignored.add(name)
            elif name.startswith(".env.") and name != ".env.example":
                ignored.add(name)
            elif name.endswith((".pyc", ".pyo", ".log", ".pem", ".key", ".crt", ".p12")):
                ignored.add(name)
        return ignored

    shutil.copytree(
        str(REPO_ROOT), str(cfg.install_dir),
        symlinks=True, dirs_exist_ok=True, ignore=ignore,
    )
    # The source tree may itself be a working copy. Never copy a live .env or
    # any generated runtime state, even if the ignore callback missed one.
    for forbidden in (cfg.install_dir / ".env", cfg.install_dir / ".runtime"):
        if forbidden.is_symlink() or forbidden.is_file():
            forbidden.unlink()
        elif forbidden.is_dir():
            shutil.rmtree(forbidden)


def wait_for(
    predicate: Callable[[], bool],
    timeout: float,
    interval: float,
    label: str,
) -> None:
    deadline = time.monotonic() + timeout
    last_note = 0.0
    while time.monotonic() < deadline:
        if predicate():
            return
        now = time.monotonic()
        if now - last_note >= 15:
            info(f"waiting for {label}…")
            last_note = now
        time.sleep(interval)
    raise InstallerError(f"timed out waiting for {label}")


class Installer:
    def __init__(self, cfg: Config, runtime: RuntimePlan):
        self.cfg = cfg
        self.plan = runtime
        self.actions: List[RollbackAction] = []
        self.gid = 0
        self.admin_created = False
        self.completed = False
        self._data_dir = Path("/var/lib/docker" if runtime.runtime == "docker" else "/var/lib/containers")
        self._data_existed = self._data_dir.exists()

    # -- package and service changes

    def install_packages(self) -> None:
        if not self.plan.install_packages:
            return
        step("Installing the container runtime")
        if not command_exists("dnf"):
            raise InstallerError("dnf is required to install the container runtime")
        if self.plan.runtime == "docker":
            repo = Path("/etc/yum.repos.d/docker-ce.repo")
            if not repo.exists():
                repo_url = (
                    "https://download.docker.com/linux/rhel/docker-ce.repo"
                    if os_release().get("ID") == "rhel"
                    else "https://download.docker.com/linux/centos/docker-ce.repo"
                )
                info("Adding the Docker CE package repository")
                dnf_install(["dnf-plugins-core"], self.actions)
                try:
                    run_command(["dnf", "config-manager", "--add-repo", repo_url])
                finally:
                    if repo.exists():
                        self.actions.append(CreatedRepoFile(repo))
            dnf_install(
                ["docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"],
                self.actions,
            )
        else:
            dnf_install(["podman", "podman-compose"], self.actions)
        info(f"{self.plan.label} installed")

    def start_runtime(self) -> None:
        if not self._data_existed:
            # Starting the runtime creates this directory. Record it before the
            # service state so rollback stops the daemon first, then removes
            # the data that only this installation could have created.
            self.actions.append(RemoveTree(
                self._data_dir, "removed container data created by this installation"
            ))
        if self.plan.runtime == "podman":
            result = run_command(["podman", "info"], capture=True, check=False)
            if result.returncode != 0:
                raise InstallerError(
                    "podman is not usable: " + ((result.stderr or "").strip().splitlines()[-1:] or ["unknown error"])[0]
                )
            return

        step("Preparing the Docker service")
        active = run_command(["systemctl", "is-active", "docker"], capture=True, check=False)
        enabled = run_command(["systemctl", "is-enabled", "docker"], capture=True, check=False)
        was_active = active.returncode == 0 and active.stdout.strip() == "active"
        was_enabled = enabled.returncode == 0 and enabled.stdout.strip() == "enabled"
        self.actions.append(DockerServiceState(was_active, was_enabled))
        if not was_active or not was_enabled:
            info("enabling and starting Docker")
            run_command(["systemctl", "enable", "--now", "docker"])
        wait_for(
            lambda: run_command(["docker", "info"], capture=True, check=False).returncode == 0,
            timeout=60, interval=2, label="the Docker daemon",
        )
        info("Docker is ready")

    # -- account and files

    def create_account(self) -> None:
        step("Creating the service account and directories")
        self.gid = ensure_group(self.cfg.service_user, self.actions)
        ensure_user(
            self.cfg.service_user, self.cfg.service_user, self.cfg.install_dir,
            self.actions,
        )
        prepare_directory(
            self.cfg.install_dir, self.actions, mode=0o750, gid=self.gid,
            label="install directory",
        )
        prepare_directory(
            self.cfg.config_dir, self.actions, mode=0o750, gid=self.gid,
            label="configuration directory",
        )
        prepare_directory(
            self.cfg.secrets_dir, self.actions, mode=0o750, gid=self.gid,
            label="secrets directory",
        )

    def install_files(self) -> None:
        step("Installing the application files")
        copy_application(self.cfg)
        run_command(
            ["chown", "-R", f"{self.cfg.service_user}:{self.cfg.service_user}",
             str(self.cfg.install_dir)]
        )
        info(f"application files installed in {self.cfg.install_dir}")

        step("Generating configuration and bootstrap secrets")
        db_password = secrets.token_hex(32)
        jwt_secret = secrets.token_hex(32)
        service_token = secrets.token_hex(32)
        # The bundled image does not enable server TLS and is reachable only
        # on the private Compose bridge. 'prefer' uses TLS if it is offered and
        # never sends the database over a published host port.
        database_url = (
            "postgresql+psycopg://patchtrack:"
            f"{db_password}@postgres:5432/patchtrack?sslmode=prefer"
        )
        write_file(
            self.cfg.env_file, build_env(self.cfg, self.cfg.service_user),
            self.actions, mode=0o640, gid=self.gid, description="created environment file",
        )
        write_secret(self.cfg.secrets_dir / "postgres_password", db_password, self.actions, self.gid)
        write_secret(self.cfg.secrets_dir / "database_url", database_url, self.actions, self.gid)
        write_secret(self.cfg.secrets_dir / "jwt_secret", jwt_secret, self.actions, self.gid)
        write_secret(self.cfg.secrets_dir / "service_token", service_token, self.actions, self.gid)

        link = self.cfg.install_dir / ".env"
        os.symlink(str(self.cfg.env_file), str(link))
        self.actions.append(CreatedPath(link, "created environment symlink"))

        metadata = {
            "version": self.cfg.version,
            "installed_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "install_dir": str(self.cfg.install_dir),
            "config_dir": str(self.cfg.config_dir),
            "secrets_dir": str(self.cfg.secrets_dir),
            "runtime": self.plan.runtime,
            "gateway_url": self.cfg.public_url,
            "admin_username": self.cfg.admin_username if self.cfg.create_admin else None,
            "verified": False,
        }
        write_file(
            self.cfg.config_dir / "install.json",
            json.dumps(metadata, indent=2, sort_keys=True) + "\n",
            self.actions, mode=0o640, gid=self.gid, description="created install metadata",
        )
        # The application tree is owned by the service account, but the
        # configuration and secret files are root-owned and group-readable.
        os.chown(self.cfg.env_file, 0, self.gid)
        info("bootstrap secrets created under " + str(self.cfg.secrets_dir))

    def mark_verified(self) -> None:
        path = self.cfg.config_dir / "install.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        data["verified"] = True
        data["verified_at"] = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            os.chmod(path, 0o640)
        except OSError:
            pass

    # -- containers

    def build_and_start(self) -> None:
        step("Building the PatchTrack image")
        present, detail = compose_project_present(self.plan.runtime)
        if present:
            raise InstallerError(detail)

        image_existed = image_exists(self.plan.runtime, self.cfg.image)
        if not image_existed:
            self.actions.append(ContainerImage(self.plan.runtime, self.cfg.image))

        if self.plan.runtime == "docker":
            config_check = compose(self.cfg, "docker", "config", "--quiet",
                                   check=False, capture=True)
            if config_check.returncode != 0:
                raise InstallerError(
                    "compose configuration is invalid: "
                    + ((config_check.stderr or config_check.stdout or "").strip())
                )
            compose(self.cfg, "docker", "build")
        else:
            run_command(
                ["podman", "build", "--format", "docker", "-t", self.cfg.image,
                 "-f", "deploy/container/Dockerfile", "."],
                cwd=self.cfg.install_dir,
            )
        info(f"image {self.cfg.image} built")

        step("Starting PostgreSQL and applying migrations")
        self.actions.append(ComposeProject(self.cfg, self.plan.runtime))
        compose(self.cfg, self.plan.runtime, "up", "-d", "postgres")
        wait_for(
            lambda: compose(
                self.cfg, self.plan.runtime, "exec", "-T", "postgres",
                "pg_isready", "-U", "patchtrack", "-d", "patchtrack",
                check=False, capture=True,
            ).returncode == 0,
            timeout=120, interval=3, label="PostgreSQL",
        )
        compose(self.cfg, self.plan.runtime, "run", "--rm", "migrate")
        info("migrations applied")

        step("Starting the PatchTrack services")
        compose(self.cfg, self.plan.runtime, "up", "-d")
        info("containers started")

    # -- verification

    def _opener(self, with_cookies: bool = False):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        handlers = [
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
        ]
        if with_cookies:
            handlers.append(urllib.request.HTTPCookieProcessor(CookieJar()))
        return urllib.request.build_opener(*handlers)

    def _get_json(self, opener, path: str, timeout: float = 10.0):
        request = urllib.request.Request(
            self.cfg.base_url + path, method="GET",
            headers={"Accept": "application/json"},
        )
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def wait_for_ready(self) -> None:
        opener = self._opener()

        def ready() -> bool:
            try:
                status, body = self._get_json(opener, "/readyz", timeout=5)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError):
                return False
            return (
                status == 200
                and body.get("status") == "ready"
                and all(value == "ok" for value in body.get("services", {}).values())
                and len(body.get("services", {})) == 4
            )

        wait_for(ready, timeout=240, interval=5, label="the gateway and all four services")

        status, health = self._get_json(opener, "/healthz")
        if status != 200 or health.get("status") != "ok":
            raise InstallerError("gateway health endpoint did not report ok")
        # The sign-in page must be served over the generated TLS certificate.
        request = urllib.request.Request(self.cfg.base_url + "/login", method="GET")
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise InstallerError("the sign-in page is not available")

    def create_administrator(self) -> None:
        if not self.cfg.create_admin:
            opener = self._opener()
            _, status = self._get_json(opener, "/api/session/status")
            if not status.get("needs_admin"):
                raise InstallerError(
                    "the platform already has an administrator; refusing to claim a fresh install"
                )
            info("no administrator created; the one-time form is available at the sign-in page")
            return

        step("Creating and verifying the first administrator")
        payload = json.dumps({
            "username": self.cfg.admin_username,
            "password": self.cfg.admin_password,
            "display_name": self.cfg.admin_display_name,
            "email": self.cfg.admin_email or None,
        }).encode("utf-8")
        opener = self._opener(with_cookies=True)
        request = urllib.request.Request(
            self.cfg.base_url + "/api/session/bootstrap",
            data=payload, method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with opener.open(request, timeout=30) as response:
                if response.status != 200:
                    raise InstallerError(
                        f"administrator creation returned HTTP {response.status}"
                    )
        except urllib.error.HTTPError as exc:
            detail = "administrator creation failed"
            try:
                body = json.loads(exc.read().decode("utf-8"))
                if isinstance(body.get("detail"), str):
                    detail += f": {body['detail']}"
            except (ValueError, UnicodeDecodeError, OSError):
                pass
            raise InstallerError(detail) from None
        _, session = self._get_json(opener, "/api/session/status")
        if not session.get("authenticated"):
            raise InstallerError("administrator was created but the verification sign-in failed")
        _, me = self._get_json(opener, "/api/me")
        if me.get("username") != self.cfg.admin_username:
            raise InstallerError("the verified session does not belong to the new administrator")
        if not me.get("must_change_password"):
            # The bootstrap account must require a password change at first
            # sign-in. If that flag is missing, the fresh-install invariant is
            # wrong and the installation should not be declared successful.
            raise InstallerError("the first administrator was not marked for a password change")
        self.admin_created = True
        info(f"administrator '{self.cfg.admin_username}' created and verified")

    def verify_permissions(self) -> None:
        env_mode = stat.S_IMODE(self.cfg.env_file.stat().st_mode)
        if env_mode & 0o022:
            raise InstallerError(f"{self.cfg.env_file} is group- or world-writable")
        if env_mode & 0o007:
            raise InstallerError(f"{self.cfg.env_file} is accessible to other users")
        directory_mode = stat.S_IMODE(self.cfg.secrets_dir.stat().st_mode)
        if directory_mode & 0o007:
            raise InstallerError(
                f"{self.cfg.secrets_dir} is accessible to other users"
            )
        for name in ("postgres_password", "database_url", "jwt_secret", "service_token"):
            path = self.cfg.secrets_dir / name
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o007:
                raise InstallerError(f"secret file {path} is accessible to other users")
            if mode & 0o020:
                raise InstallerError(f"secret file {path} is group-writable")

    def verify(self) -> None:
        step("Verifying the installation")
        self.verify_permissions()
        self.wait_for_ready()
        info("HTTPS gateway and all four backend services are healthy")
        self.create_administrator()
        self.mark_verified()

    # -- orchestration

    def run(self) -> None:
        self.install_packages()
        self.start_runtime()
        self.create_account()
        self.install_files()
        self.build_and_start()
        self.verify()
        self.completed = True

    def rollback(self) -> bool:
        if not self.actions:
            return True
        print()
        warn("Installation failed. Reversing the changes made by this installer…")
        failures: List[str] = []
        for action in reversed(self.actions):
            try:
                info(f"rollback: {action.description}")
                action.rollback()
            except Exception as exc:  # noqa: BLE001 - report and continue reversing
                failures.append(f"{action.description}: {exc}")
        print()
        if failures:
            error("Automatic rollback could not reverse every change:")
            for failure in failures:
                print(f"    - {failure}", file=sys.stderr)
            print("  Review the messages above before retrying.", file=sys.stderr)
            return False
        info("All installer changes have been reverted.")
        return True


# ------------------------------------------------------------------------ flow

def config_from_env(args: argparse.Namespace) -> Config:
    def env(name: str, default: str) -> str:
        return os.environ.get(name, default)

    cfg = Config()
    cfg.install_dir = clean_path(env("PATCHTRACK_INSTALL_DIR", str(cfg.install_dir)))
    cfg.config_dir = clean_path(env("PATCHTRACK_INSTALL_CONFIG_DIR", str(cfg.config_dir)))
    cfg.service_user = env("PATCHTRACK_INSTALL_SERVICE_USER", cfg.service_user)
    cfg.bind_address = env("PATCHTRACK_INSTALL_BIND_ADDRESS", cfg.bind_address)
    port = env("PATCHTRACK_INSTALL_GATEWAY_PORT", str(cfg.gateway_port))
    try:
        cfg.gateway_port = int(port, 10)
    except ValueError:
        cfg.gateway_port = -1
    cfg.tls_hostname = env("PATCHTRACK_INSTALL_TLS_HOSTNAME", cfg.tls_hostname)
    cfg.admin_username = env("PATCHTRACK_INSTALL_ADMIN_USERNAME", cfg.admin_username)
    cfg.admin_display_name = env(
        "PATCHTRACK_INSTALL_ADMIN_DISPLAY_NAME", cfg.admin_display_name
    )
    cfg.admin_email = env("PATCHTRACK_INSTALL_ADMIN_EMAIL", cfg.admin_email)
    cfg.admin_password = os.environ.get("PATCHTRACK_INSTALL_ADMIN_PASSWORD", "")
    cfg.runtime = args.runtime or env("PATCHTRACK_INSTALL_RUNTIME", "auto")
    try:
        cfg.min_disk_gb = int(env("PATCHTRACK_INSTALL_MIN_DISK_GB", str(cfg.min_disk_gb)))
        cfg.min_memory_gb = int(env("PATCHTRACK_INSTALL_MIN_MEMORY_GB", str(cfg.min_memory_gb)))
    except ValueError:
        cfg.min_disk_gb = -1
    cfg.allow_permissive = args.allow_permissive
    cfg.allow_unsupported_os = args.allow_unsupported_os
    cfg.create_admin = bool(cfg.admin_password)
    return cfg


def prompt_config(cfg: Config, runtime: RuntimeInfo) -> None:
    print()
    print(colour("PatchTrack guided installation", "1;1"))
    print("    Press Enter to accept a value shown in brackets.")
    cfg.install_dir = clean_path(_ask(
        "Application directory", str(cfg.install_dir), validate_install_path
    ))
    cfg.config_dir = clean_path(_ask(
        "Configuration directory", str(cfg.config_dir),
        lambda value: validate_install_path(value, "configuration directory"),
    ))
    cfg.service_user = _ask("Service account", cfg.service_user, validate_service_user)
    cfg.bind_address = _ask(
        "Address to publish the gateway on (0.0.0.0 = all interfaces)",
        cfg.bind_address, validate_bind,
    )
    cfg.gateway_port = int(_ask("HTTPS gateway port", str(cfg.gateway_port), validate_port))
    cfg.tls_hostname = _ask(
        "Hostname or IP for the generated certificate", cfg.tls_hostname, validate_hostname
    )
    print("    The first administrator is created now so the installation can be verified.")
    cfg.create_admin = _ask_yes_no("Create the first administrator?", True)
    if cfg.create_admin:
        cfg.admin_username = _ask("Administrator username", cfg.admin_username, validate_admin_username)
        cfg.admin_display_name = _ask(
            "Administrator display name", cfg.admin_display_name,
            lambda value: None if len(value) <= 200 else "must be at most 200 characters",
        )
        cfg.admin_email = _ask(
            "Administrator email (optional)", cfg.admin_email, validate_email
        )
        cfg.admin_password = _ask_password()
    else:
        cfg.admin_password = ""

    if runtime.docker_ready and runtime.podman_ready and cfg.runtime == "auto":
        choice = _ask("Container runtime (docker or podman)", "docker",
                      lambda value: None if value in ("docker", "podman") else "choose docker or podman")
        cfg.runtime = choice


def choose_runtime(cfg: Config, runtime: RuntimeInfo, assume_yes: bool) -> RuntimePlan:
    requested = cfg.runtime
    if requested == "auto":
        if runtime.docker_ready:
            requested = "docker"
        elif runtime.podman_ready:
            requested = "podman"
        else:
            requested = "docker"

    if requested == "docker":
        if runtime.docker_ready:
            return RuntimePlan("docker", install_packages=False)
        if assume_yes or _ask_yes_no(
            "Docker Engine and Compose v2 are not installed. Install them with dnf?", True
        ):
            return RuntimePlan(
                "docker", install_packages=True,
                packages=["docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"],
            )
        raise UserCancelled("container runtime installation was declined")

    if requested == "podman":
        if runtime.podman_ready:
            return RuntimePlan("podman", install_packages=False)
        if assume_yes or _ask_yes_no(
            "Podman or podman-compose is not installed. Install them with dnf?", True
        ):
            return RuntimePlan("podman", install_packages=True,
                               packages=["podman", "podman-compose"])
        raise UserCancelled("container runtime installation was declined")

    raise InstallerError("runtime must be auto, docker or podman")


def validate_config(cfg: Config) -> List[str]:
    problems = []
    for label, path in (
        ("application directory", str(cfg.install_dir)),
        ("configuration directory", str(cfg.config_dir)),
    ):
        problem = validate_install_path(path, label)
        if problem:
            problems.append(problem)
    if cfg.config_dir == cfg.install_dir:
        problems.append("application and configuration directories must differ")
    elif paths_overlap(cfg.install_dir, cfg.config_dir):
        problems.append(
            "application and configuration directories must not contain each other"
        )
    for label, value, validator in (
        ("service account", cfg.service_user, validate_service_user),
        ("bind address", cfg.bind_address, validate_bind),
        ("gateway port", str(cfg.gateway_port), validate_port),
        ("TLS hostname", cfg.tls_hostname, validate_hostname),
        ("administrator username", cfg.admin_username, validate_admin_username),
        ("administrator email", cfg.admin_email, validate_email),
    ):
        problem = validator(value)
        if problem:
            problems.append(f"{label} {problem}")
    if cfg.create_admin and not (MIN_PASSWORD <= len(cfg.admin_password) <= MAX_PASSWORD):
        problems.append(
            f"administrator password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters"
        )
    if cfg.runtime not in ("auto", "docker", "podman"):
        problems.append("runtime must be auto, docker or podman")
    if cfg.min_disk_gb < 1 or cfg.min_memory_gb < 1:
        problems.append("minimum disk and memory must be at least 1")
    return problems


def summary(cfg: Config, plan: RuntimePlan) -> None:
    print()
    print(colour("Installation summary", "1;1"))
    print(f"    Application directory : {cfg.install_dir}")
    print(f"    Configuration         : {cfg.config_dir}")
    print(f"    Service account       : {cfg.service_user}")
    print(f"    Runtime               : {plan.label}")
    print(f"    Gateway               : {cfg.bind_address}:{cfg.gateway_port} (HTTPS)")
    print(f"    Certificate for       : {cfg.tls_hostname}")
    print(f"    Administrator         : "
          f"{cfg.admin_username if cfg.create_admin else 'not created (web form at first sign-in)'}")
    print(f"    Public URL            : {cfg.public_url}")
    print("    On failure, every change recorded by this run is reversed automatically.")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="install.sh",
        description="Guided PatchTrack installation with preflight checks and automatic rollback.",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="run all preflight dependency checks and exit without changing anything",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help=(
            "non-interactive: use defaults and PATCHTRACK_INSTALL_* environment variables. "
            "The first administrator is only created when PATCHTRACK_INSTALL_ADMIN_PASSWORD is set."
        ),
    )
    parser.add_argument(
        "--runtime", choices=("auto", "docker", "podman"), default=None,
        help="container runtime to use (default: auto)",
    )
    parser.add_argument(
        "--allow-permissive", action="store_true",
        help="continue when SELinux is not enforcing (documented exceptions only)",
    )
    parser.add_argument(
        "--allow-unsupported-os", action="store_true",
        help="skip the RHEL/Rocky 9 and architecture requirements",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    cfg = config_from_env(args)
    runtime = detect_runtime()

    if args.check:
        if cfg.runtime == "podman" or (
            cfg.runtime == "auto" and not runtime.docker_ready and runtime.podman_ready
        ):
            plan = RuntimePlan("podman", install_packages=not runtime.podman_ready)
        elif cfg.runtime == "docker":
            plan = RuntimePlan("docker", install_packages=not runtime.docker_ready)
        else:
            plan = RuntimePlan("docker", install_packages=not runtime.docker_ready)
        print(colour("PatchTrack preflight checks", "1;1"))
        problems = validate_config(cfg)
        if problems:
            for problem in problems:
                error(problem)
            return 1
        failures, warnings = print_checks(collect_preflight(cfg, runtime, plan))
        print()
        if failures:
            error(f"{failures} required check(s) failed.")
            return 1
        if warnings:
            warn(f"{warnings} warning(s); see the notes above.")
        else:
            info("All required checks passed.")
        return 0

    if not args.yes:
        if not sys.stdin.isatty():
            error("interactive input is unavailable; use --yes with PATCHTRACK_INSTALL_* variables")
            return 2
        try:
            prompt_config(cfg, runtime)
        except (UserCancelled, KeyboardInterrupt) as exc:
            print()
            if str(exc):
                error(str(exc))
            info("Installation cancelled; nothing has been changed.")
            return 130 if isinstance(exc, KeyboardInterrupt) else 2
    else:
        if not cfg.create_admin:
            info("No administrator will be created (set PATCHTRACK_INSTALL_ADMIN_PASSWORD to create one).")

    problems = validate_config(cfg)
    if problems:
        for problem in problems:
            error(problem)
        return 2

    try:
        plan = choose_runtime(cfg, runtime, assume_yes=args.yes)
    except UserCancelled as exc:
        error(str(exc))
        return 2

    checks = collect_preflight(cfg, runtime, plan)
    print()
    print(colour("Preflight checks", "1;1"))
    failures, warnings = print_checks(checks)
    if failures:
        print()
        error(f"{failures} required precheck(s) failed; nothing has been changed.")
        return 1
    if warnings:
        print()
        warn(f"{warnings} warning(s) — the installation may still work if mirrors or caches are available.")

    summary(cfg, plan)
    if not args.yes and not _ask_yes_no("Proceed with the installation?", True):
        info("Installation cancelled; nothing has been changed.")
        return 0

    installer = Installer(cfg, plan)

    def _terminate(signum, frame):  # noqa: ANN001
        raise KeyboardInterrupt

    old_term = signal.signal(signal.SIGTERM, _terminate)

    try:
        installer.run()
    except KeyboardInterrupt:
        print()
        error("Installation interrupted.")
        installer.rollback()
        return 130
    except InstallerError as exc:
        print()
        error(str(exc))
        installer.rollback()
        return 1
    except Exception as exc:  # noqa: BLE001 - unexpected failures must still roll back
        print()
        error(f"unexpected installation failure: {exc}")
        installer.rollback()
        return 1
    finally:
        signal.signal(signal.SIGTERM, old_term)

    if not installer.completed:
        return 1

    print()
    print(colour("PatchTrack is installed and verified.", "1;32"))
    print(f"    Open: {cfg.public_url}")
    if cfg.create_admin:
        print(f"    Administrator: {cfg.admin_username}")
        print("    You will be asked to change that password at first sign-in.")
    else:
        print("    No administrator exists yet; use the one-time form on the sign-in page.")
    print("    The certificate is self-signed until an administrator uploads the organisation's")
    print("    certificate at Settings → HTTPS certificate.")
    print(f"    Configuration: {cfg.env_file}")
    print(f"    Secrets:       {cfg.secrets_dir}")
    if cfg.runtime == "docker":
        print(f"    Logs: cd {cfg.install_dir} && docker compose -f docker-compose.yml "
              "-f docker-compose.secrets.yml logs -f")
    else:
        print(f"    Logs: cd {cfg.install_dir} && podman-compose -f docker-compose.yml "
              "-f docker-compose.secrets.yml logs -f")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
