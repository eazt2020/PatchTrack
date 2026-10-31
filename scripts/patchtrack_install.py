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
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_VERSION = "3.0.7"
DEFAULT_INSTALL_DIR = Path("/opt/patchtrack")
DEFAULT_CONFIG_DIR = Path("/etc/patchtrack")
DEFAULT_SERVICE_USER = "patchtrack"
DEFAULT_BIND = "0.0.0.0"
DEFAULT_PORT = 8005
COMPOSE_PROJECT = "patchtrack"
SAMPLE_DATA_ASSETS = 100
SAMPLE_DATA_YEARS = 5
MAX_SAMPLE_ASSETS = 10_000
MAX_SAMPLE_YEARS = 10
MIN_PASSWORD = 12
MAX_PASSWORD = 512
DEFAULT_NO_PROXY = (
    "localhost,127.0.0.1,::1,postgres,identity,inventory,compliance,"
    "reporting,gateway,migrate"
)
DOCKER_PROXY_DROPIN = Path(
    "/etc/systemd/system/docker.service.d/10-patchtrack-proxy.conf"
)
SYSTEMD_SERVICE_NAME = "patchtrack.service"
SYSTEMD_UNIT_PATH = Path("/etc/systemd/system") / SYSTEMD_SERVICE_NAME
SYSTEMD_UNIT_MARKER = "Managed by the PatchTrack guided installer"
AIRGAP_SUMS = "SHA256SUMS"
AIRGAP_POSTGRES_IMAGE = "docker.io/library/postgres:17"
AIRGAP_BASE_ARCHIVE = "ubi9-python-312.tar"
AIRGAP_POSTGRES_ARCHIVE = "postgres-17.tar"

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
    if not value:
        return None
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
        return "must be a valid email address, or left empty"
    # The identity service validates this field with pydantic's EmailStr,
    # which refuses the reserved suffixes below. Rejecting them here keeps a
    # fresh installation from passing preflight and then failing after the
    # stack is already running.
    suffix = value.rsplit("@", 1)[1].lower().rstrip(".").rsplit(".", 1)[-1]
    if suffix in ("arpa", "invalid", "local", "localhost", "onion", "test"):
        return (
            "must not use a reserved domain suffix (.arpa, .invalid, .local, "
            ".localhost, .onion or .test); use a real domain or leave it empty"
        )
    return None


def validate_proxy(value: str) -> Optional[str]:
    """Accept an empty value (direct access) or an http(s) proxy URL."""
    if not value:
        return None
    if any(character.isspace() or ord(character) < 32 for character in value):
        return "must not contain whitespace or control characters"
    if any(character in value for character in "\"'`\\"):
        return "must not contain quotes, backticks or backslashes"
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return "is not a valid proxy URL"
    if parts.scheme not in ("http", "https"):
        return "must start with http:// or https://"
    if not parts.hostname:
        return "must include a proxy host name or address"
    try:
        port = parts.port
    except ValueError:
        return "has an invalid port"
    if port is not None and not 1 <= port <= 65535:
        return "has an invalid port"
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return "must be only scheme, credentials, host and optional port"
    return None


def validate_bundle_dir(value: str) -> Optional[str]:
    if not value:
        return None
    return _path_error(value)


def validate_sample_assets(value: str) -> Optional[str]:
    try:
        count = int(value, 10)
    except ValueError:
        return "must be a whole number"
    if not 1 <= count <= MAX_SAMPLE_ASSETS:
        return f"must be between 1 and {MAX_SAMPLE_ASSETS}"
    return None


def validate_sample_years(value: str) -> Optional[str]:
    try:
        years = int(value, 10)
    except ValueError:
        return "must be a whole number"
    if not 1 <= years <= MAX_SAMPLE_YEARS:
        return f"must be between 1 and {MAX_SAMPLE_YEARS}"
    return None


def env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def bundle_archives(version: str) -> dict:
    return {
        "app": f"patchtrack-app-{version}.tar",
        "base": AIRGAP_BASE_ARCHIVE,
        "postgres": AIRGAP_POSTGRES_ARCHIVE,
    }


def _sums_entries(sums: Path) -> set:
    """The file names listed in a SHA256SUMS file."""
    entries = set()
    try:
        lines = sums.read_text().splitlines()
    except OSError:
        return entries
    for line in lines:
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            name = parts[1].lstrip("*")
            entries.add(name[2:] if name.startswith("./") else name)
    return entries


def verify_bundle(bundle_dir: Path, required: Sequence[str] = ()) -> Tuple[bool, str]:
    """Verify the bundle's SHA256SUMS, including the required archives.

    ``--ignore-missing`` keeps an optional artifact that the operator removed
    from failing the checksum check; its presence is checked separately.
    Required archives must be listed so an empty or partial manifest cannot
    vouch for nothing.
    """
    sums = bundle_dir / AIRGAP_SUMS
    if not sums.is_file():
        return False, f"{sums} is missing; the bundle cannot be verified"
    listed = _sums_entries(sums)
    absent = [name for name in required if name not in listed]
    if absent:
        return False, f"{absent[0]} is not listed in {AIRGAP_SUMS}"
    result = run_command(
        ["sha256sum", "-c", "--ignore-missing", AIRGAP_SUMS],
        cwd=bundle_dir, check=False, capture=True,
    )
    # Per-file results go to stdout; warnings and missing-file errors go to
    # stderr. Prefer naming the file that failed over the generic warning.
    results = (result.stdout or "").strip().splitlines()
    errors = (result.stderr or "").strip().splitlines()
    bad = [line for line in results if not line.rstrip().endswith(": OK")]
    if result.returncode != 0 or bad:
        if bad:
            detail = bad[0]
        elif errors:
            detail = errors[-1]
        else:
            detail = "checksum verification failed"
        return False, detail[:300]
    return True, f"{len(results)} file(s) verified"


def bundle_preflight_checks(
    cfg: Config, plan: RuntimePlan, runtime_ready: bool
) -> List[Check]:
    """Checks for an air-gapped host, where network probes are irrelevant."""
    checks = [Check(
        "air-gap mode",
        True,
        "network access is not required; images come from the bundle or the local runtime",
    )]
    runtime = plan.runtime
    archives = bundle_archives(cfg.version)

    if cfg.bundle_dir:
        bundle = cfg.bundle_dir
        required = (archives["app"], archives["postgres"])
        checks.append(Check(
            "air-gap bundle",
            bundle.is_dir(),
            str(bundle) if bundle.is_dir() else f"{bundle} is not a directory",
            remediation=(
                "Extract the release bundle to that path, or pass the correct "
                "directory with --bundle-dir (or PATCHTRACK_INSTALL_BUNDLE_DIR)."
            ),
        ))
        if bundle.is_dir():
            ok, detail = verify_bundle(bundle, required)
            checks.append(Check(
                "bundle checksums", ok, detail,
                remediation=(
                    "Re-copy the bundle from the release media and verify it "
                    "with 'sha256sum -c SHA256SUMS'; do not install from a "
                    "bundle that fails verification."
                ),
            ))
            for label, name in (
                ("application", archives["app"]),
                ("PostgreSQL", archives["postgres"]),
            ):
                checks.append(Check(
                    f"{label} image archive",
                    (bundle / name).is_file(),
                    str(bundle / name) if (bundle / name).is_file()
                    else f"{bundle / name} is missing",
                    remediation=(
                        f"Use a complete bundle for version {cfg.version}; the "
                        f"{label.lower()} image archive is required for an "
                        "air-gap installation."
                    ),
                ))
            for label, name in (
                ("pinned base image", archives["base"]),
                ("source archive", f"patchtrack-{cfg.version}.tar.gz"),
                ("Python wheels", "patchtrack-wheels"),
            ):
                present = (bundle / name).exists()
                checks.append(Check(
                    label,
                    True,
                    "present in the bundle" if present else
                    "not present in the bundle; only needed for rebuilds or native installs",
                    required=False,
                    warning=not present,
                    remediation=(
                        f"Add {name} to the bundle on the connected host if this "
                        "host must rebuild the image, run a native install or use "
                        "the optional dashboards; otherwise no action is needed."
                    ),
                ))
    else:
        app_present = runtime_ready and image_exists(runtime, cfg.image)
        postgres_present = runtime_ready and image_exists(
            runtime, AIRGAP_POSTGRES_IMAGE
        )
        checks.append(Check(
            "preloaded application image",
            app_present,
            f"{cfg.image} is loaded" if app_present else
            f"{cfg.image} is not loaded; pass --bundle-dir or load the bundle first",
            remediation=(
                f"Pass a verified --bundle-dir, or load the application archive "
                f"with '{runtime} load -i {archives['app']}' before rerunning."
            ),
        ))
        checks.append(Check(
            "preloaded PostgreSQL image",
            postgres_present,
            f"{AIRGAP_POSTGRES_IMAGE} is loaded" if postgres_present else
            f"{AIRGAP_POSTGRES_IMAGE} is not loaded; pass --bundle-dir or load the bundle first",
            remediation=(
                f"Pass a verified --bundle-dir, or load the PostgreSQL archive "
                f"with '{runtime} load -i {archives['postgres']}' before rerunning."
            ),
        ))

    if plan.install_packages:
        repo_check = run_command(
            ["dnf", "-q", "repolist", "--enabled"], capture=True, check=False
        ) if command_exists("dnf") else None
        repos = [
            line for line in (repo_check.stdout or "").splitlines()[1:]
            if line.strip()
        ] if repo_check is not None and repo_check.returncode == 0 else []
        checks.append(Check(
            "local package repository",
            bool(repos),
            f"{len(repos)} enabled repository/repositories; confirm they are local, "
            "because air-gap mode does not add a remote Docker repository"
            if repos else
            "no enabled dnf repository can supply the container runtime",
            remediation=(
                "Enable a local dnf repository containing docker-ce and "
                "docker-compose-plugin (or podman and podman-compose), or "
                "preinstall the runtime and rerun the check."
            ),
        ))
    return checks


def validate_no_proxy(value: str) -> Optional[str]:
    if not value:
        return None
    for token in value.split(","):
        token = token.strip()
        if not token:
            return "must not contain empty entries"
        if not re.fullmatch(r"[A-Za-z0-9.*_:\[\]/-]+", token):
            return f"contains an invalid host or domain: {token}"
    return None


def display_proxy(value: str) -> str:
    """Render a proxy URL without exposing credentials."""
    if not value:
        return "direct access"
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        return "(invalid proxy URL)"
    if parts.username is None and parts.password is None:
        return value
    host = parts.hostname or "proxy"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://***@{host}{port}"


def proxy_environment(cfg: "Config") -> dict:
    if not cfg.proxy_url:
        return {}
    value = cfg.proxy_url
    return {
        "HTTP_PROXY": value,
        "HTTPS_PROXY": value,
        "http_proxy": value,
        "https_proxy": value,
        "NO_PROXY": cfg.no_proxy,
        "no_proxy": cfg.no_proxy,
    }


def command_environment(cfg: "Config") -> Optional[dict]:
    """The installer's environment, with the proxy applied when configured."""
    if not cfg.proxy_url:
        return None
    environment = os.environ.copy()
    environment.update(proxy_environment(cfg))
    # BuildKit forwards the proxy variables into RUN steps without placing the
    # URL (which may contain credentials) on a command line.
    environment.setdefault("DOCKER_BUILDKIT", "1")
    return environment


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
    proxy_url: str = field(default="", repr=False)
    no_proxy: str = DEFAULT_NO_PROXY
    airgap: bool = False
    bundle_dir: Optional[Path] = None
    sample_data: bool = False
    sample_assets: int = SAMPLE_DATA_ASSETS
    sample_years: int = SAMPLE_DATA_YEARS
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
    # What the operator can do about it. Printed only when the check fails or
    # warns, so a passing check does not add noise to the preflight report.
    remediation: str = ""


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
            checks.append(Check(
                label, False, f"{path} is a symbolic link; use a real directory",
                remediation=f"Choose a real directory for {label} instead of a symlink.",
            ))
        elif exists and not path.is_dir():
            checks.append(Check(
                label, False, f"{path} is not a directory",
                remediation=f"Remove or rename the file at {path}, or choose another path.",
            ))
        elif exists and nonempty:
            checks.append(Check(
                label, False, f"{path} is not empty; refusing to overwrite it",
                remediation=(
                    f"Use a new empty directory for {label}, or remove the previous "
                    "installation after confirming it is no longer needed."
                ),
            ))
        else:
            checks.append(Check(label, True, f"{path} is available"))
    if SYSTEMD_UNIT_PATH.exists() or SYSTEMD_UNIT_PATH.is_symlink():
        checks.append(Check(
            "systemd service", False,
            f"{SYSTEMD_UNIT_PATH} already exists; refusing to overwrite it",
            remediation=(
                f"Remove or rename {SYSTEMD_UNIT_PATH}, then run "
                "systemctl daemon-reload. If it belongs to a previous PatchTrack "
                "installation, remove that installation first."
            ),
        ))
    else:
        checks.append(Check(
            "systemd service", True, f"{SYSTEMD_UNIT_PATH} is available"
        ))
    problem = validate_install_path(str(cfg.install_dir), "install directory")
    if problem:
        checks.append(Check(
            "install path", False, problem,
            remediation=(
                "Use an absolute path outside the source tree and system "
                "directories, such as /opt/patchtrack."
            ),
        ))
    problem = validate_install_path(str(cfg.config_dir), "configuration directory")
    if problem:
        checks.append(Check(
            "configuration path", False, problem,
            remediation=(
                "Use an absolute path outside the source tree and system "
                "directories, such as /etc/patchtrack."
            ),
        ))
    if cfg.config_dir == cfg.install_dir:
        checks.append(Check(
            "path separation", False,
            "configuration and install directories must differ",
            remediation=(
                "Set PATCHTRACK_INSTALL_DIR and PATCHTRACK_INSTALL_CONFIG_DIR "
                "to different directories."
            ),
        ))
    elif paths_overlap(cfg.install_dir, cfg.config_dir):
        checks.append(Check(
            "path separation", False,
            "configuration and install directories must not contain each other",
            remediation=(
                "Move the configuration directory outside the application "
                "directory (or vice versa) so the two trees are separate."
            ),
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


def network_reachable(url: str, proxy_url: str = "") -> bool:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "PatchTrack-installer"})
    handlers = []
    if proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=8) as response:
            response.read(1)
            return True
    except urllib.error.HTTPError as exc:
        # Registry endpoints commonly answer 401/403 to an unauthenticated
        # request. That still proves the endpoint is reachable.
        return exc.code in (401, 403)
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def proxy_reachable(proxy_url: str) -> Tuple[bool, str]:
    """Check that the proxy accepts a TCP connection."""
    try:
        parts = urllib.parse.urlsplit(proxy_url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False, "the proxy URL is invalid"
    host = parts.hostname
    if not host:
        return False, "the proxy URL has no host"
    try:
        with socket.create_connection((host, port), timeout=5):
            return True, f"{host}:{port} accepts connections"
    except OSError as exc:
        return False, f"cannot connect to {host}:{port}: {exc}"


def collect_preflight(cfg: Config, runtime: RuntimeInfo, plan: RuntimePlan) -> List[Check]:
    checks: List[Check] = []

    checks.append(Check(
        "root privileges",
        os.geteuid() == 0,
        "running as root" if os.geteuid() == 0 else "rerun with sudo",
        remediation=(
            "Rerun the installer with sudo: root is required to create the "
            "service account, directories and service."
        ),
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
        remediation=(
            "Install on RHEL 9 or Rocky Linux 9. Use --allow-unsupported-os "
            "only for a documented exception."
        ),
    ))

    machine = os.uname().machine
    checks.append(Check(
        "architecture",
        machine in ("x86_64", "aarch64") or cfg.allow_unsupported_os,
        machine,
        remediation=(
            "Run on an x86_64 or aarch64 host. Use --allow-unsupported-os only "
            "for a documented exception."
        ),
    ))

    cores = os.cpu_count() or 1
    checks.append(Check(
        "CPU", cores >= cfg.min_cpu, f"{cores} vCPU (minimum {cfg.min_cpu})",
        remediation=f"Allocate at least {cfg.min_cpu} vCPUs to this host.",
    ))

    memory = memory_bytes()
    checks.append(Check(
        "memory",
        memory >= cfg.min_memory_gb * 1024 ** 3,
        f"{memory / 1024 ** 3:.1f} GiB (minimum {cfg.min_memory_gb} GiB)",
        remediation=(
            f"Allocate at least {cfg.min_memory_gb} GiB of memory, or set "
            "PATCHTRACK_INSTALL_MIN_MEMORY_GB to a lower accepted value if the "
            "deployment is sized for it."
        ),
    ))

    enforcing = selinux_enforcing()
    if enforcing is None:
        selinux_ok = cfg.allow_unsupported_os
        selinux_detail = "SELinux status could not be read"
        selinux_fix = (
            "Ensure SELinux is enabled in the kernel and /sys/fs/selinux is "
            "mounted; investigate the host before installing."
        )
    elif enforcing:
        selinux_ok = True
        selinux_detail = "enforcing"
        selinux_fix = ""
    else:
        selinux_ok = cfg.allow_permissive
        selinux_detail = "not enforcing; use --allow-permissive only for a documented exception"
        selinux_fix = (
            "Set SELinux to enforcing in /etc/selinux/config and reboot. Use "
            "--allow-permissive only for a documented exception."
        )
    checks.append(Check("SELinux", selinux_ok, selinux_detail, remediation=selinux_fix))

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
            remediation=(
                f"Free at least {cfg.min_disk_gb} GiB on the filesystem holding "
                f"{path}, move that path to a larger filesystem, or set "
                "PATCHTRACK_INSTALL_MIN_DISK_GB to a lower accepted value if the "
                "deployment is sized for it."
            ),
        ))

    required_commands = ["useradd", "groupadd", "systemctl"]
    if plan.install_packages:
        required_commands.append("dnf")
    if plan.runtime == "docker":
        required_commands.append("systemctl")
    command_packages = {
        "useradd": "shadow-utils",
        "groupadd": "shadow-utils",
        "systemctl": "systemd",
        "dnf": "dnf",
    }
    for command in sorted(set(required_commands)):
        package = command_packages.get(command, command)
        checks.append(Check(
            f"command {command}", command_exists(command),
            "found" if command_exists(command) else f"{command} is required",
            remediation=(
                f"Install the {package} package with dnf, or ensure {command} "
                "is present on PATH before rerunning."
            ),
        ))

    if plan.install_packages:
        checks.append(Check(
            "package manager",
            command_exists("dnf"),
            "dnf is available" if command_exists("dnf") else "dnf is required to install the runtime",
            remediation=(
                "Install dnf on the host, or install Docker/Podman yourself and "
                "rerun the installer."
            ),
        ))

    ready = runtime.docker_ready if plan.runtime == "docker" else runtime.podman_ready
    if plan.install_packages:
        runtime_detail = f"{plan.label}; dnf will install it"
        runtime_ok = command_exists("dnf")
        runtime_fix = (
            "Install dnf so the installer can install Docker Engine with "
            "Compose v2, or preinstall a supported runtime."
        )
    elif ready:
        version = runtime.docker_version if plan.runtime == "docker" else runtime.podman_version
        runtime_detail = f"{plan.label} {version or ''}".strip()
        runtime_ok = True
        runtime_fix = ""
    else:
        runtime_detail = f"{plan.label} is incomplete"
        runtime_ok = False
        runtime_fix = (
            "Install Docker Engine with the Compose v2 plugin, or Podman with "
            "podman-compose, then rerun. Use --runtime podman to select Podman "
            "explicitly."
        )
    checks.append(Check(
        "container runtime", runtime_ok, runtime_detail, remediation=runtime_fix
    ))

    free, detail = port_available(cfg.bind_address, cfg.gateway_port)
    checks.append(Check(
        "gateway port",
        free,
        f"{cfg.bind_address}:{cfg.gateway_port} is {detail}",
        remediation=(
            f"Stop the process using {cfg.bind_address}:{cfg.gateway_port}, or "
            "choose another port with PATCHTRACK_INSTALL_GATEWAY_PORT."
        ),
    ))

    checks.extend(target_path_checks(cfg))

    if ready:
        present, detail = compose_project_present(plan.runtime)
        checks.append(Check(
            "existing compose project", not present, detail,
            remediation=(
                "Use upgrade.sh to update the existing installation, or remove "
                "the old PatchTrack compose project and volumes after confirming "
                "they are no longer needed."
            ),
        ))

    if cfg.airgap:
        checks.extend(bundle_preflight_checks(cfg, plan, ready))
        if cfg.proxy_url:
            checks.append(Check(
                "proxy in air-gap mode",
                True,
                "configured, but no registry or package download is expected",
                required=False,
                warning=True,
                remediation=(
                    "No action is needed: air-gap mode does not use the proxy. "
                    "Unset PATCHTRACK_INSTALL_PROXY if it is not required."
                ),
            ))
        return checks

    if cfg.proxy_url:
        reachable, detail = proxy_reachable(cfg.proxy_url)
        checks.append(Check(
            "HTTP proxy", reachable, f"{display_proxy(cfg.proxy_url)}: {detail}",
            remediation=(
                "Check the proxy address, credentials and firewall, then correct "
                "PATCHTRACK_INSTALL_PROXY; unset it to use direct access."
            ),
        ))
        checks.append(Check(
            "proxy bypass list",
            validate_no_proxy(cfg.no_proxy) is None,
            cfg.no_proxy or "empty (all destinations use the proxy)",
            required=False,
            remediation=(
                "Use a comma-separated list of host names or domains without "
                "schemes or spaces, for example "
                "'localhost,127.0.0.1,.internal'."
            ),
        ))

    network_checks = [
        ("container image registry", "https://registry-1.docker.io/v2/"),
        ("base image registry", "https://registry.access.redhat.com/v2/"),
        ("Python package index", "https://pypi.org/simple/"),
    ]
    if plan.install_packages:
        network_checks.append(("Docker package repository", "https://download.docker.com/linux/"))
    network_fixes = {
        "container image registry": (
            "Allow HTTPS to https://registry-1.docker.io (set "
            "PATCHTRACK_INSTALL_PROXY if access is through a proxy), or preload "
            "the PostgreSQL image from a verified air-gap bundle."
        ),
        "base image registry": (
            "Allow HTTPS to https://registry.access.redhat.com (set "
            "PATCHTRACK_INSTALL_PROXY if access is through a proxy), or include "
            "the pinned base image in a verified air-gap bundle."
        ),
        "Python package index": (
            "Allow HTTPS to https://pypi.org (set PATCHTRACK_INSTALL_PROXY if "
            "access is through a proxy), or include the patchtrack-wheels "
            "directory in a verified air-gap bundle."
        ),
        "Docker package repository": (
            "Allow HTTPS to https://download.docker.com or configure a local "
            "dnf mirror; alternatively preinstall Docker or Podman and rerun. "
            "On an air-gapped host, enable a local repository containing the "
            "runtime packages."
        ),
    }
    route = " through the proxy" if cfg.proxy_url else ""
    for label, url in network_checks:
        reachable = network_reachable(url, cfg.proxy_url)
        checks.append(Check(
            label,
            True,
            f"reachable{route}" if reachable else
            f"not reachable{route} right now; the build may fail unless images and "
            "packages are mirrored or cached",
            required=False,
            warning=not reachable,
            remediation=network_fixes[label],
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
        if not check.ok or check.warning:
            fix = check.remediation or (
                "Review the detail above, correct the host configuration, and "
                "rerun --check."
            )
            label = colour(
                "how to fix", "1;33" if check.warning else "1;31"
            )
            print(f"        {label}: {fix}")
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


def docker_proxy_unit(cfg: Config) -> str:
    """The systemd drop-in that lets the Docker daemon pull through the proxy."""
    environment = proxy_environment(cfg)
    definitions = []
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        # '%' starts a systemd specifier even inside a quoted Environment value.
        value = environment[name].replace("%", "%%")
        definitions.append(f'"{name}={value}"')
    return (
        "[Service]\nEnvironment=" + " ".join(definitions) + "\n"
        "# Managed by the PatchTrack guided installer; remove with uninstall.sh\n"
        "# --remove-proxy-config when the host no longer needs this proxy.\n"
    )


def compose_service_unit(install_dir: Path, runtime: str) -> str:
    """The single systemd service that owns the Compose stack.

    A normal administrator starts and stops PatchTrack with
    ``systemctl start|stop patchtrack`` and reads the stack's output with
    ``journalctl -u patchtrack``. The service runs Compose in the foreground so
    systemd's active state reflects the running stack rather than the result of
    a one-shot command.
    """
    if runtime == "docker":
        compose = "/usr/bin/docker compose"
        after = "network-online.target docker.service"
        requires = "Requires=docker.service\n"
    else:
        compose = "/usr/bin/podman-compose"
        after = "network-online.target"
        requires = ""
    files = "-f docker-compose.yml -f docker-compose.secrets.yml"
    return (
        f"# {SYSTEMD_UNIT_MARKER} for {install_dir}.\n"
        "#\n"
        "# Start, stop and inspect the application with systemctl and\n"
        "# journalctl. Do not run compose directly while this unit is active;\n"
        "# use systemctl restart after changing .env or the Compose files.\n"
        "[Unit]\n"
        "Description=PatchTrack application\n"
        f"Documentation=file:{install_dir}/docs/start-stop.md\n"
        "Wants=network-online.target\n"
        f"After={after}\n"
        f"{requires}"
        f"ConditionPathExists={install_dir}/docker-compose.yml\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={install_dir}\n"
        f"ExecStart={compose} {files} up --remove-orphans\n"
        f"ExecStop={compose} {files} stop\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "TimeoutStartSec=300\n"
        "TimeoutStopSec=180\n"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def systemd_unit_is_installer_owned(text: str, install_dir: Path) -> bool:
    """True when a unit was written for this installation, not by an operator."""
    return (
        SYSTEMD_UNIT_MARKER in text
        and f"WorkingDirectory={install_dir}" in text
    )


class SystemdServiceUnit(RollbackAction):
    """Disable and remove the Compose service unit during rollback."""

    def __init__(self, path: Path):
        self.path = path
        self.description = f"systemd service: {path}"

    def rollback(self) -> None:
        if command_exists("systemctl"):
            run_command(
                ["systemctl", "disable", "--now", self.path.name],
                check=False, capture=True,
            )
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        if command_exists("systemctl"):
            run_command(["systemctl", "daemon-reload"], check=False, capture=True)


class DockerProxyConfig(RollbackAction):
    """Remove the proxy drop-in and restart Docker when rolling back."""

    def __init__(self, path: Path):
        self.path = path
        self.description = f"Docker proxy configuration: {path}"

    def rollback(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        if not command_exists("systemctl"):
            raise InstallerError(
                "systemctl is unavailable; remove the Docker proxy drop-in and restart Docker manually"
            )
        reload_result = run_command(["systemctl", "daemon-reload"], check=False, capture=True)
        if reload_result.returncode != 0:
            raise InstallerError(
                "could not reload systemd: " + (reload_result.stderr or "").strip()
            )
        active = run_command(["systemctl", "is-active", "docker"], capture=True, check=False)
        if active.returncode == 0 and active.stdout.strip() == "active":
            restart = run_command(["systemctl", "restart", "docker"], check=False, capture=True)
            if restart.returncode != 0:
                raise InstallerError(
                    "could not restart Docker after removing its proxy configuration: "
                    + (restart.stderr or "").strip()
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
        self.description = f"container image: {image}"

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


def dnf_install(
    cfg: Config, packages: Sequence[str], actions: List[RollbackAction]
) -> None:
    before_txn = latest_dnf_transaction()
    before_packages = rpm_packages()
    try:
        run_command(
            ["dnf", "-y", "install"] + list(packages),
            env=command_environment(cfg),
        )
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
    return run_command(
        command, cwd=cfg.install_dir, check=check, capture=capture,
        env=command_environment(cfg),
    )


def sample_data_command(
    cfg: Config,
    runtime: str,
    script: str,
    *args: str,
    reports_volume: Optional[str] = None,
    image: Optional[str] = None,
    project: str = COMPOSE_PROJECT,
) -> List[str]:
    """Build a one-off container that runs a sample-data maintenance script.

    The application image does not contain ``scripts/``, so the installed
    script is bind-mounted read-only. The secret files are mounted
    individually, as Compose does: the configuration directory is not
    traversable by the container's unprivileged user. The database URL and JWT
    secret are read from files, so no credential appears on the command line
    or in the container environment.
    """
    script_path = cfg.install_dir / "scripts" / script
    command = [
        runtime, "run", "--rm",
        "--network", f"{project}_default",
        "--volume",
        f"{cfg.secrets_dir / 'database_url'}:/run/secrets/database_url:ro,z",
        "--volume",
        f"{cfg.secrets_dir / 'jwt_secret'}:/run/secrets/jwt_secret:ro,z",
        "--volume",
        f"{script_path}:/opt/patchtrack/scripts/{script}:ro,z",
    ]
    if reports_volume:
        command += [
            "--volume", f"{reports_volume}:/var/lib/patchtrack/reports:z",
        ]
    command += [
        "--env", "PATCHTRACK_DATABASE_URL_FILE=/run/secrets/database_url",
        "--env", "PATCHTRACK_JWT_SECRET_FILE=/run/secrets/jwt_secret",
        image or cfg.image, "python", f"/opt/patchtrack/scripts/{script}",
        *args,
    ]
    return command


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
{proxy_block}"""


def env_proxy_block(cfg: Config) -> str:
    if not cfg.proxy_url:
        return ""
    # Single quotes stop Compose from interpolating '$' characters in a
    # password. The services and the compose network bypass the proxy.
    value = cfg.proxy_url
    no_proxy = cfg.no_proxy
    return (
        "# Outbound proxy for integrations and SMTP. Internal compose services and\n"
        "# loopback bypass it through NO_PROXY. This file is mode 0640 root:<group>;\n"
        "# use a proxy without credentials, or a credential the host may store here.\n"
        f"HTTP_PROXY='{value}'\n"
        f"HTTPS_PROXY='{value}'\n"
        f"NO_PROXY='{no_proxy}'\n"
        f"http_proxy='{value}'\n"
        f"https_proxy='{value}'\n"
        f"no_proxy='{no_proxy}'\n"
    )


def build_env(cfg: Config, group: str) -> str:
    return ENV_TEMPLATE.format(
        date=_dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        secrets_dir=cfg.secrets_dir,
        group=group,
        version=cfg.version,
        bind=cfg.bind_address,
        port=cfg.gateway_port,
        hostname=cfg.tls_hostname,
        proxy_block=env_proxy_block(cfg),
    )


def copy_application(cfg: Config, source: Optional[Path] = None) -> None:
    source_tree = source or REPO_ROOT

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
        str(source_tree), str(cfg.install_dir),
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
        self.created_user = False
        self.created_group = False
        self.prebuilt_image = False
        self.image_loaded = False
        self.sample_loaded = False
        self.sample_error = ""
        # Docker keeps containers under /var/lib/docker and, with the
        # containerd image store, images under /var/lib/containerd. Podman
        # uses /var/lib/containers for both. Record which of these the host
        # already had, so rollback removes only data this installation created.
        if runtime.runtime == "docker":
            self._data_dirs = (Path("/var/lib/docker"), Path("/var/lib/containerd"))
        else:
            self._data_dirs = (Path("/var/lib/containers"),)
        self._data_existed = {path: path.exists() for path in self._data_dirs}

    # -- package and service changes

    def install_packages(self) -> None:
        if not self.plan.install_packages:
            return
        step("Installing the container runtime")
        if not command_exists("dnf"):
            raise InstallerError("dnf is required to install the container runtime")
        if self.plan.runtime == "docker":
            repo = Path("/etc/yum.repos.d/docker-ce.repo")
            if not repo.exists() and not self.cfg.airgap:
                repo_url = (
                    "https://download.docker.com/linux/rhel/docker-ce.repo"
                    if os_release().get("ID") == "rhel"
                    else "https://download.docker.com/linux/centos/docker-ce.repo"
                )
                info("Adding the Docker CE package repository")
                dnf_install(self.cfg, ["dnf-plugins-core"], self.actions)
                try:
                    run_command(
                        ["dnf", "config-manager", "--add-repo", repo_url],
                        env=command_environment(self.cfg),
                    )
                finally:
                    if repo.exists():
                        self.actions.append(CreatedRepoFile(repo))
            elif not repo.exists():
                info(
                    "air-gap mode: using the host's configured dnf repositories; "
                    "no remote Docker CE repository will be added"
                )
            dnf_install(
                self.cfg,
                ["docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"],
                self.actions,
            )
        else:
            dnf_install(self.cfg, ["podman", "podman-compose"], self.actions)
        info(f"{self.plan.label} installed")

    def start_runtime(self) -> None:
        for path in self._data_dirs:
            if self._data_existed[path]:
                continue
            # Starting the runtime creates this directory. Record it before the
            # service state so rollback stops the daemon first, then removes
            # the data that only this installation could have created.
            self.actions.append(RemoveTree(
                path, "removed container data created by this installation"
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
        self.configure_docker_proxy()
        info("Docker is ready")

    def configure_docker_proxy(self) -> None:
        """Let the daemon pull base images when the host uses a proxy."""
        if self.plan.runtime != "docker" or not self.cfg.proxy_url:
            return
        current = run_command(
            ["systemctl", "show", "docker", "--property=Environment"],
            capture=True, check=False,
        )
        if current.returncode == 0 and re.search(
            r"(?i)\bhttps?_proxy=", current.stdout
        ):
            info("Docker daemon already has a proxy configured; leaving it unchanged")
            return
        path = DOCKER_PROXY_DROPIN
        if path.exists() or path.is_symlink():
            warn(
                f"{path} already exists; leaving it unchanged. Verify that it points "
                f"at {display_proxy(self.cfg.proxy_url)} before the image build."
            )
            return
        info("Configuring the Docker daemon to use " + display_proxy(self.cfg.proxy_url))
        prepare_directory(
            path.parent, self.actions, mode=0o755, gid=0,
            label="Docker service configuration directory",
        )
        write_file(
            path, docker_proxy_unit(self.cfg), self.actions, mode=0o644,
            description="created Docker proxy configuration",
        )
        self.actions.append(DockerProxyConfig(path))
        run_command(["systemctl", "daemon-reload"])
        info("restarting Docker to apply the proxy configuration")
        run_command(["systemctl", "restart", "docker"])
        wait_for(
            lambda: run_command(["docker", "info"], capture=True, check=False).returncode == 0,
            timeout=60, interval=2, label="the Docker daemon with the proxy configuration",
        )

    # -- account and files

    def create_account(self) -> None:
        step("Creating the service account and directories")
        try:
            grp.getgrnam(self.cfg.service_user)
            self.created_group = False
        except KeyError:
            self.created_group = True
        try:
            pwd.getpwnam(self.cfg.service_user)
            self.created_user = False
        except KeyError:
            self.created_user = True
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
        # Compose bind-mounts a file secret with the host file's ownership and
        # mode, and the application image runs as UID 1001 / GID 0. Group root
        # therefore lets the containers read the file while keeping it
        # unreadable to every other host account (and to the unprivileged
        # patchtrack service user used only by the native deployment).
        write_secret(self.cfg.secrets_dir / "postgres_password", db_password, self.actions, 0)
        write_secret(self.cfg.secrets_dir / "database_url", database_url, self.actions, 0)
        write_secret(self.cfg.secrets_dir / "jwt_secret", jwt_secret, self.actions, 0)
        write_secret(self.cfg.secrets_dir / "service_token", service_token, self.actions, 0)

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
            "service_user": self.cfg.service_user,
            "created_user": self.created_user,
            "created_group": self.created_group,
            "image": self.cfg.image,
            "compose_project": "patchtrack",
            "gateway_url": self.cfg.public_url,
            "installation_mode": "airgap" if self.cfg.airgap else "online",
            "airgap_bundle": str(self.cfg.bundle_dir) if self.cfg.airgap and self.cfg.bundle_dir else None,
            "admin_username": self.cfg.admin_username if self.cfg.create_admin else None,
            "sample_data": {
                "enabled": self.cfg.sample_data,
                "assets": self.cfg.sample_assets if self.cfg.sample_data else 0,
                "years": self.cfg.sample_years if self.cfg.sample_data else 0,
                "loaded": False,
                "loaded_at": None,
            },
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

    def _update_metadata(self, **updates) -> None:
        path = self.cfg.config_dir / "install.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return
        data.update(updates)
        try:
            path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
            os.chmod(path, 0o640)
        except OSError:
            pass

    def mark_verified(self) -> None:
        self._update_metadata(
            verified=True,
            verified_at=_dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        )

    def install_systemd_unit(self) -> None:
        step("Installing the systemd service")
        if SYSTEMD_UNIT_PATH.exists() or SYSTEMD_UNIT_PATH.is_symlink():
            raise InstallerError(
                f"{SYSTEMD_UNIT_PATH} already exists; refusing to overwrite it"
            )
        unit = compose_service_unit(self.cfg.install_dir, self.plan.runtime)
        # Record the rollback first: a partial write must still be cleaned up.
        self.actions.append(SystemdServiceUnit(SYSTEMD_UNIT_PATH))
        write_file(
            SYSTEMD_UNIT_PATH, unit, [], mode=0o644,
            description=f"created systemd service: {SYSTEMD_UNIT_PATH}",
        )
        run_command(["systemctl", "daemon-reload"])
        run_command(["systemctl", "enable", SYSTEMD_SERVICE_NAME])
        info(f"systemd service {SYSTEMD_SERVICE_NAME} is enabled")

    def activate_systemd_unit(self) -> None:
        step("Registering the running application with systemd")
        result = run_command(
            ["systemctl", "start", SYSTEMD_SERVICE_NAME],
            check=False, capture=True,
        )
        if result.returncode != 0:
            raise InstallerError(
                f"could not start {SYSTEMD_SERVICE_NAME}: "
                + ((result.stderr or result.stdout or "").strip() or "unknown error")
            )
        info(f"{SYSTEMD_SERVICE_NAME} is active; use systemctl to manage it")

    # -- containers

    def load_archive(self, archive: Path) -> None:
        if not archive.is_file():
            raise InstallerError(f"{archive} is missing from the air-gap bundle")
        runtime = self.plan.runtime
        run_command(
            [runtime, "load", "-i", str(archive)],
            env=command_environment(self.cfg),
        )

    def tag_loaded_application(self) -> None:
        """Make the bundle's application image visible as patchtrack:<version>."""
        runtime = self.plan.runtime
        if image_exists(runtime, self.cfg.image):
            return
        version = self.cfg.version
        candidates = (
            f"docker.io/library/patchtrack:{version}",
            f"localhost/patchtrack:{version}",
            f"patchtrack:{version}",
        )
        for candidate in candidates:
            if not image_exists(runtime, candidate):
                continue
            target = (
                f"localhost/patchtrack:{version}"
                if runtime == "podman" else self.cfg.image
            )
            run_command([runtime, "tag", candidate, target])
            if image_exists(runtime, self.cfg.image):
                if runtime == "podman" and candidate != target:
                    # Keep only the tag the manifest and uninstaller use.
                    run_command(
                        ["podman", "image", "rm", candidate],
                        check=False, capture=True,
                    )
                info(f"tagged {candidate} as {self.cfg.image}")
                return
        raise InstallerError(
            f"the bundle did not provide {self.cfg.image}; expected one of "
            + ", ".join(candidates)
        )

    def prepare_images(self) -> None:
        if not self.cfg.airgap:
            return
        step("Preparing the air-gap images")
        runtime = self.plan.runtime
        archives = bundle_archives(self.cfg.version)

        if self.cfg.bundle_dir:
            # The bundle is the verified source of both images. Loading it
            # replaces a stale image that happens to carry the same tag.
            existed = image_exists(runtime, self.cfg.image)
            self.load_archive(self.cfg.bundle_dir / archives["app"])
            self.image_loaded = True
            if not existed:
                self.actions.append(ContainerImage(runtime, self.cfg.image))
            self.tag_loaded_application()

            self.load_archive(self.cfg.bundle_dir / archives["postgres"])
            info(f"loaded {AIRGAP_POSTGRES_IMAGE} from the bundle")
        else:
            for label, image in (
                ("application", self.cfg.image),
                ("PostgreSQL", AIRGAP_POSTGRES_IMAGE),
            ):
                if not image_exists(runtime, image):
                    raise InstallerError(
                        f"{image} is not loaded and no --bundle-dir was given"
                    )
                info(f"{image} is already loaded ({label})")

        self.prebuilt_image = True
        info(f"using {self.cfg.image} loaded from the air-gap bundle"
             if self.image_loaded else f"using {self.cfg.image} already present")

    def compose_up(self, *targets: str) -> subprocess.CompletedProcess:
        args = ["up", "-d"]
        if self.cfg.airgap and self.plan.runtime == "docker":
            args += ["--pull", "never"]
        args += list(targets)
        return compose(self.cfg, self.plan.runtime, *args)

    def build_and_start(self) -> None:
        present, detail = compose_project_present(self.plan.runtime)
        if present:
            raise InstallerError(detail)

        if self.prebuilt_image:
            step("Using the preloaded PatchTrack image")
            info(f"{self.cfg.image} will not be rebuilt")
        else:
            step("Building the PatchTrack image")
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
            if not self.prebuilt_image:
                compose(self.cfg, "docker", "build")
        elif not self.prebuilt_image:
            run_command(
                ["podman", "build", "--format", "docker", "-t", self.cfg.image,
                 "-f", "deploy/container/Dockerfile", "."],
                cwd=self.cfg.install_dir,
                env=command_environment(self.cfg),
            )
        if self.prebuilt_image:
            info(f"image {self.cfg.image} is ready")
        else:
            info(f"image {self.cfg.image} built")

        step("Starting PostgreSQL and applying migrations")
        self.actions.append(ComposeProject(self.cfg, self.plan.runtime))
        self.compose_up("postgres")
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
        self.compose_up()
        info("containers started")

    def load_sample_data(self) -> None:
        if not self.cfg.sample_data:
            return
        step("Loading synthetic sample data")
        info(
            f"seeding {self.cfg.sample_assets} sample servers and "
            f"{self.cfg.sample_years} years of history; every record is marked synthetic"
        )
        command = sample_data_command(
            self.cfg, self.plan.runtime, "seed-demo.py",
            "--assets", str(self.cfg.sample_assets),
            "--years", str(self.cfg.sample_years),
        )
        # Stream the seed's progress: generating years of history can take a
        # few minutes, and a silent installer looks hung.
        result = run_command(
            command, check=False, capture=False,
            env=command_environment(self.cfg),
        )
        if result.returncode != 0:
            self.sample_error = f"seed-demo exited with code {result.returncode}"
            warn(
                "sample data could not be loaded; the installation itself is "
                "unaffected and will continue"
            )
            warn("see the seed output above for the reason")
            self._update_metadata(sample_data={
                "enabled": True,
                "assets": self.cfg.sample_assets,
                "years": self.cfg.sample_years,
                "loaded": False,
                "loaded_at": None,
                "error": self.sample_error,
            })
            return
        self.sample_loaded = True
        self._update_metadata(sample_data={
            "enabled": True,
            "assets": self.cfg.sample_assets,
            "years": self.cfg.sample_years,
            "loaded": True,
            "loaded_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        })
        info(
            "sample data loaded; remove it later with "
            "uninstall.sh --purge-sample-data"
        )

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
            # /readyz reports the four application services plus the database
            # and backup worker, which the console displays but which do not
            # decide readiness. Require the application set by name rather
            # than counting entries, so a new informational service cannot
            # make a healthy installation fail its verification.
            services = body.get("services") or {}
            required = ("identity", "inventory", "compliance", "reporting")
            return (
                status == 200
                and body.get("status") == "ready"
                and all(services.get(name) == "ok" for name in required)
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
            detail = f"administrator creation failed (HTTP {exc.code})"
            try:
                body = json.loads(exc.read().decode("utf-8"))
                problem = body.get("detail")
                if isinstance(problem, str):
                    detail += f": {problem}"
                elif isinstance(problem, list):
                    # FastAPI validation errors arrive as a list of objects.
                    messages = [
                        item.get("msg", "") for item in problem if isinstance(item, dict)
                    ]
                    messages = [message for message in messages if message]
                    if messages:
                        detail += ": " + "; ".join(messages)
            except (ValueError, UnicodeDecodeError, OSError, AttributeError):
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
        self.prepare_images()
        self.install_files()
        self.install_systemd_unit()
        self.build_and_start()
        self.load_sample_data()
        self.verify()
        self.activate_systemd_unit()
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
    cfg.proxy_url = env("PATCHTRACK_INSTALL_PROXY", cfg.proxy_url)
    cfg.no_proxy = env("PATCHTRACK_INSTALL_NO_PROXY", cfg.no_proxy)
    cfg.airgap = bool(args.airgap) or env_bool("PATCHTRACK_INSTALL_AIRGAP")
    bundle = args.bundle_dir or os.environ.get("PATCHTRACK_INSTALL_BUNDLE_DIR", "")
    if bundle:
        cfg.bundle_dir = clean_path(bundle)
        cfg.airgap = True
    cfg.admin_username = env("PATCHTRACK_INSTALL_ADMIN_USERNAME", cfg.admin_username)
    cfg.admin_display_name = env(
        "PATCHTRACK_INSTALL_ADMIN_DISPLAY_NAME", cfg.admin_display_name
    )
    cfg.admin_email = env("PATCHTRACK_INSTALL_ADMIN_EMAIL", cfg.admin_email)
    cfg.admin_password = os.environ.get("PATCHTRACK_INSTALL_ADMIN_PASSWORD", "")
    cfg.sample_data = bool(
        args.sample_data
        or args.sample_assets is not None
        or args.sample_years is not None
    ) or env_bool("PATCHTRACK_INSTALL_SAMPLE_DATA")
    if args.sample_assets is not None:
        cfg.sample_assets = args.sample_assets
    if args.sample_years is not None:
        cfg.sample_years = args.sample_years
    for name, attribute in (
        ("PATCHTRACK_INSTALL_SAMPLE_ASSETS", "sample_assets"),
        ("PATCHTRACK_INSTALL_SAMPLE_YEARS", "sample_years"),
    ):
        value = os.environ.get(name)
        if value is not None:
            try:
                setattr(cfg, attribute, int(value, 10))
            except ValueError:
                setattr(cfg, attribute, -1)
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
    cfg.proxy_url = _ask(
        "HTTP(S) proxy for package and image downloads (optional, blank for direct access)",
        cfg.proxy_url, validate_proxy,
    )
    if cfg.proxy_url:
        cfg.no_proxy = _ask(
            "Hosts that must bypass the proxy", cfg.no_proxy, validate_no_proxy
        )
    cfg.airgap = _ask_yes_no(
        "Is this host air-gapped (no internet access)?", cfg.airgap
    )
    if cfg.airgap:
        bundle = _ask(
            "Air-gap bundle directory (blank if the images are already loaded)",
            str(cfg.bundle_dir) if cfg.bundle_dir else "",
            validate_bundle_dir,
        )
        cfg.bundle_dir = clean_path(bundle) if bundle else None
    else:
        cfg.bundle_dir = None
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

    print("    Sample data is synthetic: it exercises the console and reports before")
    print("    the integrations are connected, and can be removed later without")
    print("    uninstalling PatchTrack (uninstall.sh --purge-sample-data).")
    cfg.sample_data = _ask_yes_no(
        "Load synthetic sample data for evaluation?", cfg.sample_data
    )
    if cfg.sample_data:
        cfg.sample_assets = int(_ask(
            "Sample servers", str(cfg.sample_assets), validate_sample_assets
        ))
        cfg.sample_years = int(_ask(
            "Years of sample history", str(cfg.sample_years), validate_sample_years
        ))

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
        ("proxy URL", cfg.proxy_url, validate_proxy),
        ("proxy bypass list", cfg.no_proxy, validate_no_proxy),
        ("air-gap bundle directory", str(cfg.bundle_dir or ""), validate_bundle_dir),
        ("administrator username", cfg.admin_username, validate_admin_username),
        ("administrator email", cfg.admin_email, validate_email),
    ):
        problem = validator(value)
        if problem:
            problems.append(f"{label} {problem}")
    if cfg.bundle_dir and (
        paths_overlap(cfg.bundle_dir, cfg.install_dir)
        or paths_overlap(cfg.bundle_dir, cfg.config_dir)
    ):
        problems.append(
            "air-gap bundle directory must not overlap the application or "
            "configuration directory"
        )
    if cfg.create_admin and not (MIN_PASSWORD <= len(cfg.admin_password) <= MAX_PASSWORD):
        problems.append(
            f"administrator password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters"
        )
    if cfg.runtime not in ("auto", "docker", "podman"):
        problems.append("runtime must be auto, docker or podman")
    if cfg.sample_data:
        for label, value, validator in (
            ("sample server count", str(cfg.sample_assets), validate_sample_assets),
            ("sample history years", str(cfg.sample_years), validate_sample_years),
        ):
            problem = validator(value)
            if problem:
                problems.append(f"{label} {problem}")
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
    print(f"    Internet proxy        : {display_proxy(cfg.proxy_url)}")
    if cfg.proxy_url:
        print(f"    Proxy bypass          : {cfg.no_proxy}")
    if cfg.airgap:
        print(f"    Air-gap bundle        : {cfg.bundle_dir or 'images already loaded'}")
    else:
        print("    Network access        : online")
    print(f"    Administrator         : "
          f"{cfg.admin_username if cfg.create_admin else 'not created (web form at first sign-in)'}")
    if cfg.sample_data:
        print(f"    Sample data           : {cfg.sample_assets} servers, "
              f"{cfg.sample_years} years (synthetic)")
    else:
        print("    Sample data           : not loaded")
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
        "--airgap", action="store_true",
        help=(
            "air-gapped installation: skip registry checks, load images from "
            "--bundle-dir and do not add a remote Docker repository"
        ),
    )
    parser.add_argument(
        "--bundle-dir", default=None, metavar="PATH",
        help="directory containing the verified air-gap bundle (implies --airgap)",
    )
    parser.add_argument(
        "--sample-data", action="store_true",
        help=(
            "load synthetic sample inventory and history after the stack starts, "
            "so the console and reports can be evaluated before the integrations "
            "are connected"
        ),
    )
    parser.add_argument(
        "--sample-assets", type=int, default=None, metavar="N",
        help=f"sample servers to create (default: {SAMPLE_DATA_ASSETS}; implies --sample-data)",
    )
    parser.add_argument(
        "--sample-years", type=int, default=None, metavar="N",
        help=f"years of synthetic history (default: {SAMPLE_DATA_YEARS}; implies --sample-data)",
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
            error(
                f"{failures} required check(s) failed. Address the 'how to fix' "
                "lines above and rerun --check."
            )
            return 1
        if warnings:
            warn(
                f"{warnings} warning(s); review the 'how to fix' lines above "
                "before installing."
            )
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
        error(
            f"{failures} required precheck(s) failed; nothing has been changed. "
            "Address the 'how to fix' lines above and rerun."
        )
        return 1
    if warnings:
        print()
        warn(
            f"{warnings} warning(s) — review the 'how to fix' lines above. The "
            "installation may still work if mirrors or caches are available."
        )

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
    if installer.sample_loaded:
        print(f"    Sample data: {cfg.sample_assets} synthetic servers with "
              f"{cfg.sample_years} years of history.")
        print("    Remove it later with: sudo ./uninstall.sh --purge-sample-data")
    elif cfg.sample_data:
        print("    Sample data was requested but was not loaded; see the warning above.")
    print("    The certificate is self-signed until an administrator uploads the organisation's")
    print("    certificate at Settings → HTTPS certificate.")
    print(f"    Configuration: {cfg.env_file}")
    print(f"    Secrets:       {cfg.secrets_dir}")
    print(f"    Start/stop:   sudo systemctl start|stop|restart {SYSTEMD_SERVICE_NAME}")
    print(f"    Status:       sudo systemctl status {SYSTEMD_SERVICE_NAME}")
    print(f"    Logs:         sudo journalctl -u {SYSTEMD_SERVICE_NAME} -f")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
