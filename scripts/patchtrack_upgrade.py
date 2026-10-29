#!/usr/bin/env python3
"""Guided PatchTrack upgrade for RHEL 9 / Rocky 9.

Run this from the unpacked directory of the new release. The script:

1. preflights the existing installation from its ``install.json`` manifest:
   root, source and target paths, versions, service account, runtime, Compose
   project, PostgreSQL, current readiness and free space;
2. takes a mandatory backup of the previous application tree, configuration
   (``.env``, secrets, manifest) and a PostgreSQL custom-format dump under
   ``/var/lib/patchtrack/upgrades``;
3. stages the new tree beside the current one, swaps it in, updates the version
   and builds the new image;
4. stops the application services, applies migrations and starts the stack;
5. verifies readiness, the sign-in page and the recorded version.

If any step after the swap fails, it restores the previous tree, ``.env`` and
manifest, restores the database dump when migrations had started, rebuilds the
previous image if necessary and starts the previous version again. The backup
is never deleted automatically.

The backup dump contains the whole database (metadata and reporting data), not
only PatchTrack tables, so the PostgreSQL password stays in the existing
secret files and is never passed on a command line.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import patchtrack_install as base  # noqa: E402
import patchtrack_uninstall as manifest_reader  # noqa: E402

DEFAULT_BACKUP_ROOT = Path("/var/lib/patchtrack/upgrades")
COMPOSE_FILES = (("-f", "docker-compose.yml"), ("-f", "docker-compose.secrets.yml"))
VERSION_RE = re.compile(r"[0-9][A-Za-z0-9._-]{0,31}")
DISK_HEADROOM = 512 * 1024 ** 2
READY_TIMEOUT = 300


class UpgradeError(RuntimeError):
    """A condition that stops the upgrade and triggers rollback."""


class UserCancelled(UpgradeError):
    """The operator chose not to continue."""


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


def read_env(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def config_for_commands(manifest: manifest_reader.Manifest) -> base.Config:
    env = read_env(manifest.env_file)
    return base.Config(
        install_dir=manifest.install_dir,
        config_dir=manifest.config_dir,
        service_user=manifest.service_user or "patchtrack",
        proxy_url=env.get("HTTPS_PROXY") or env.get("HTTP_PROXY") or "",
        no_proxy=env.get("NO_PROXY") or base.DEFAULT_NO_PROXY,
    )


def probe_url(manifest: manifest_reader.Manifest) -> str:
    env = read_env(manifest.env_file)
    bind = env.get("PATCHTRACK_BIND_ADDRESS", "0.0.0.0")
    host = "127.0.0.1" if bind in ("", "0.0.0.0") else bind
    port = env.get("PATCHTRACK_GATEWAY_PORT", "8005")
    tls = env.get("PATCHTRACK_TLS_ENABLED", "true").lower() not in (
        "0", "false", "no", "off"
    )
    return f"{'https' if tls else 'http'}://{host}:{port}"


def opener():
    context = base.ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = base.ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
    )


def get_json(open_url, url: str, timeout: float = 10.0):
    request = urllib.request.Request(
        url, method="GET", headers={"Accept": "application/json"}
    )
    with open_url.open(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def installation_ready(url: str) -> Tuple[bool, str]:
    open_url = opener()
    try:
        status, body = get_json(open_url, url + "/readyz", timeout=8)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return False, str(exc)
    services = body.get("services", {})
    if status == 200 and body.get("status") == "ready" and len(services) == 4 \
            and all(value == "ok" for value in services.values()):
        return True, "all four services are ready"
    return False, f"readyz returned {status} with {len(services)} service(s)"


def wait_for_ready(url: str, label: str) -> None:
    def ready() -> bool:
        return installation_ready(url)[0]

    base.wait_for(ready, timeout=READY_TIMEOUT, interval=5, label=label)


def version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def validate_upgrade_version(current: str, new: str) -> Optional[str]:
    if not new:
        return "a target version is required (--version or PATCHTRACK_UPGRADE_VERSION)"
    if not VERSION_RE.fullmatch(new):
        return "must start with a digit and contain only letters, digits, '.', '_' or '-'"
    if new == current:
        return f"the installation is already at {current}"
    current_key = version_key(current)
    new_key = version_key(new)
    if current_key and new_key:
        width = max(len(current_key), len(new_key))
        left = current_key + (0,) * (width - len(current_key))
        right = new_key + (0,) * (width - len(new_key))
        if right < left:
            return (
                f"{new} is older than {current}; downgrades require a database "
                "restore, not an upgrade"
            )
    return None


def systemd_unit_owned(manifest: manifest_reader.Manifest) -> bool:
    """True when this installation owns the Compose systemd service."""
    try:
        text = base.SYSTEMD_UNIT_PATH.read_text()
    except OSError:
        return False
    return base.systemd_unit_is_installer_owned(text, manifest.install_dir)


def compose_argv(manifest: manifest_reader.Manifest, *args: str) -> List[str]:
    prefix = ["docker", "compose"] if manifest.runtime == "docker" else ["podman-compose"]
    for pair in COMPOSE_FILES:
        prefix.extend(pair)
    return prefix + list(args)


def run_stream(
    argv: Sequence[str],
    cwd: Path,
    *,
    stdout=None,
    stdin=None,
    env: Optional[dict] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(arg) for arg in argv],
        cwd=str(cwd),
        stdin=stdin,
        stdout=stdout,
        stderr=subprocess.PIPE,
        env=env,
    )


def directory_size(path: Path) -> int:
    total = 0
    for root, directories, files in os.walk(path):
        directories[:] = [
            name for name in directories
            if name not in (".git", ".runtime", "__pycache__", ".pytest_cache")
        ]
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                pass
    return total


def staging_paths(install_dir: Path) -> Tuple[Path, Path]:
    return (
        Path(str(install_dir) + ".upgrade-new"),
        Path(str(install_dir) + ".upgrade-old"),
    )


def replace_env_version(path: Path, version: str) -> None:
    """Atomically change PATCHTRACK_VERSION, preserving ownership and mode."""
    current = path.stat()
    text = path.read_text()
    pattern = re.compile(r"^PATCHTRACK_VERSION=.*$", re.MULTILINE)
    if pattern.search(text):
        updated = pattern.sub(f"PATCHTRACK_VERSION={version}", text)
    else:
        updated = text.rstrip("\n") + f"\nPATCHTRACK_VERSION={version}\n"
    fd, temporary = tempfile.mkstemp(prefix=".env.upgrade-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(updated)
        os.chmod(temporary, stat.S_IMODE(current.st_mode))
        os.chown(temporary, current.st_uid, current.st_gid)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def write_manifest(path: Path, data: dict) -> None:
    current = path.stat()
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=".install-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.chmod(temporary, stat.S_IMODE(current.st_mode))
        os.chown(temporary, current.st_uid, current.st_gid)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


# ------------------------------------------------------------------ preflight

@dataclass
class Context:
    manifest: manifest_reader.Manifest
    data: dict
    current_version: str
    new_version: str
    source: Path
    backup_root: Path
    staging: Path
    previous: Path
    probe: str
    cfg: base.Config


@dataclass
class RuntimeState:
    containers: List[str]
    postgres_running: bool
    old_image_present: bool
    install_size: int
    disk_free: int


def build_context(args: argparse.Namespace) -> Context:
    manifest = manifest_reader.load_manifest(
        base.clean_path(str(args.config_dir)) / "install.json"
    )
    try:
        data = json.loads(manifest.path.read_text())
    except (OSError, ValueError) as exc:
        raise UpgradeError(f"cannot read {manifest.path}: {exc}") from None
    source = base.clean_path(str(args.source_dir)).resolve()
    current = str(data.get("version") or manifest.version)
    new = args.version or os.environ.get("PATCHTRACK_UPGRADE_VERSION") or ""
    staging, previous = staging_paths(manifest.install_dir)
    backup_root = base.clean_path(str(args.backup_dir))
    if base.paths_overlap(backup_root, manifest.install_dir) or \
            base.paths_overlap(backup_root, manifest.config_dir):
        raise UpgradeError(
            "the backup directory must not be inside the application or "
            "configuration directory"
        )
    return Context(
        manifest=manifest,
        data=data,
        current_version=current,
        new_version=new,
        source=source,
        backup_root=backup_root,
        staging=staging,
        previous=previous,
        probe=probe_url(manifest),
        cfg=config_for_commands(manifest),
    )


def inspect_project(ctx: Context) -> RuntimeState:
    manifest = ctx.manifest
    containers = manifest_reader._project_items(manifest, "containers") or []
    postgres = base.compose(
        ctx.cfg, manifest.runtime, "ps", "-q", "postgres",
        check=False, capture=True,
    )
    return RuntimeState(
        containers=containers,
        postgres_running=bool(postgres.stdout.strip()),
        old_image_present=base.image_exists(manifest.runtime, manifest.image),
        install_size=directory_size(manifest.install_dir),
        disk_free=base.free_bytes(ctx.backup_root),
    )


def collect_preflight(ctx: Context) -> Tuple[List[base.Check], RuntimeState]:
    checks: List[base.Check] = []
    manifest = ctx.manifest

    checks.append(base.Check(
        "root privileges",
        os.geteuid() == 0,
        "running as root" if os.geteuid() == 0 else "rerun with sudo",
    ))
    checks.append(base.Check(
        "installation manifest", True, str(manifest.path),
    ))

    for label, path in (
        ("application directory", manifest.install_dir),
        ("configuration directory", manifest.config_dir),
    ):
        if path.is_symlink():
            checks.append(base.Check(label, False, f"{path} is a symbolic link"))
        elif not path.is_dir():
            checks.append(base.Check(label, False, f"{path} is not a directory"))
        else:
            checks.append(base.Check(label, True, str(path)))

    compose_present = (
        (manifest.install_dir / "docker-compose.yml").is_file()
        and (manifest.install_dir / "docker-compose.secrets.yml").is_file()
    )
    checks.append(base.Check(
        "current installation",
        compose_present,
        "compose files are present" if compose_present
        else f"{manifest.install_dir} does not look like a PatchTrack installation",
    ))

    source_ok = (
        ctx.source != manifest.install_dir
        and (ctx.source / "docker-compose.yml").is_file()
        and (ctx.source / "services").is_dir()
    )
    checks.append(base.Check(
        "new release directory",
        source_ok,
        str(ctx.source) if source_ok
        else "the directory this script runs from is not an unpacked PatchTrack release",
    ))
    try:
        ctx.source.relative_to(manifest.install_dir)
        inside = True
    except ValueError:
        inside = False
    checks.append(base.Check(
        "source and target are separate",
        not inside,
        "the new release must not be inside the installed tree"
        if inside else "separate directories",
    ))

    problem = validate_upgrade_version(ctx.current_version, ctx.new_version)
    checks.append(base.Check(
        "target version",
        problem is None,
        f"{ctx.current_version} → {ctx.new_version}" if problem is None else problem,
    ))

    user = manifest.service_user or manifest_reader.infer_service_user(manifest)
    try:
        base.pwd.getpwnam(user)
        user_ok, user_detail = True, f"{user} exists"
    except (KeyError, TypeError):
        user_ok, user_detail = False, f"service account {user or '(none)'} does not exist"
    checks.append(base.Check("service account", user_ok, user_detail))

    ready, detail = manifest_reader.compose_ready(manifest)
    checks.append(base.Check("container runtime", ready, detail))
    state = inspect_project(ctx) if ready else RuntimeState([], False, False, 0, 0)
    checks.append(base.Check(
        "compose project",
        bool(state.containers),
        f"{len(state.containers)} container(s) running or stopped"
        if state.containers else "no PatchTrack containers were found; use install.sh instead",
    ))
    checks.append(base.Check(
        "PostgreSQL container",
        state.postgres_running,
        "running" if state.postgres_running
        else "the postgres service is not running; the upgrade needs it for the backup",
    ))
    current_up, current_detail = installation_ready(ctx.probe)
    checks.append(base.Check(
        "current installation is healthy",
        current_up,
        current_detail,
    ))
    checks.append(base.Check(
        "previous image is available",
        True,
        manifest.image + (" is present" if state.old_image_present
                          else " is not present; rollback will rebuild it from the backup"),
        required=False,
        warning=not state.old_image_present,
    ))

    for label, path in (("staging directory", ctx.staging), ("previous-tree directory", ctx.previous)):
        checks.append(base.Check(
            label,
            not path.exists() and not path.is_symlink(),
            str(path) if not path.exists() else f"{path} already exists; remove it after checking it",
        ))

    if manifest.env_file.is_symlink():
        checks.append(base.Check("environment file", False, "must be a regular file"))
    elif not manifest.env_file.is_file():
        checks.append(base.Check("environment file", False, f"{manifest.env_file} is missing"))
    else:
        checks.append(base.Check("environment file", True, str(manifest.env_file)))

    required = state.install_size * 2 + DISK_HEADROOM
    checks.append(base.Check(
        "free space for backup and staging",
        state.disk_free >= required,
        f"{state.disk_free / 1024 ** 3:.1f} GiB free, "
        f"{required / 1024 ** 3:.1f} GiB needed at {ctx.backup_root}",
    ))
    return checks, state


# --------------------------------------------------------------------- backup

@dataclass
class Backup:
    root: Path
    app: Path
    config: Path
    database: Path
    metadata: Path


def create_backup(ctx: Context) -> Backup:
    stamp = _dt.datetime.now().strftime("%Y%m%dT%H%M%S")
    root = ctx.backup_root / f"upgrade-{stamp}"
    if root.exists():
        raise UpgradeError(f"backup directory {root} already exists")
    root.mkdir(parents=True, mode=0o700)
    try:
        return _write_backup(ctx, root)
    except Exception:
        # A failed backup is not a usable artefact. Remove the partial copy so
        # the next attempt starts clean; nothing in the deployment changed.
        shutil.rmtree(str(root), ignore_errors=True)
        raise


def _write_backup(ctx: Context, root: Path) -> Backup:
    app = root / "app"
    config = root / "config"
    database = root / "database.dump"
    metadata = root / "upgrade.json"

    base.info(f"backing up the application tree to {app}")
    shutil.copytree(
        str(ctx.manifest.install_dir), str(app),
        symlinks=True, copy_function=shutil.copy2,
    )
    base.info(f"backing up the configuration to {config}")
    shutil.copytree(
        str(ctx.manifest.config_dir), str(config),
        symlinks=True, copy_function=shutil.copy2,
    )

    base.info("dumping the database")
    env = base.command_environment(ctx.cfg)
    with open(database, "wb") as handle:
        result = run_stream(
            compose_argv(
                ctx.manifest, "exec", "-T", "postgres", "pg_dump",
                "-U", "patchtrack", "-d", "patchtrack", "-Fc",
            ),
            ctx.manifest.install_dir,
            stdout=handle,
            env=env,
        )
    if result.returncode != 0 or database.stat().st_size == 0:
        detail = (result.stderr or b"").decode("utf-8", "replace").strip()
        raise UpgradeError("database backup failed" + (f": {detail}" if detail else ""))

    metadata.write_text(json.dumps({
        "created_at": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "from_version": ctx.current_version,
        "to_version": ctx.new_version,
        "image": ctx.manifest.image,
        "install_dir": str(ctx.manifest.install_dir),
        "config_dir": str(ctx.manifest.config_dir),
    }, indent=2, sort_keys=True) + "\n")
    os.chmod(metadata, 0o600)
    base.info(f"backup complete: {root}")
    return Backup(root=root, app=app, config=config, database=database, metadata=metadata)


# -------------------------------------------------------------------- upgrade

class Upgrade:
    def __init__(self, ctx: Context, backup: Backup):
        self.ctx = ctx
        self.backup = backup
        self.swapped = False
        self.version_changed = False
        self.services_stopped = False
        self.db_touched = False
        self.unit_owned = systemd_unit_owned(ctx.manifest)
        self.failures: List[str] = []

    def _compose(self, *args: str, **kwargs):
        return base.compose(
            self.ctx.cfg, self.ctx.manifest.runtime, *args, **kwargs
        )

    def _psql(self, sql: str):
        """Run one statement against the maintenance database."""
        return self._compose(
            "exec", "-T", "postgres", "psql", "-U", "patchtrack",
            "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", sql,
            check=False, capture=True,
        )

    def stage(self) -> None:
        base.step("Staging the new application tree")
        if self.ctx.staging.exists() or self.ctx.staging.is_symlink():
            raise UpgradeError(f"{self.ctx.staging} already exists")
        staging_cfg = base.Config(
            install_dir=self.ctx.staging,
            config_dir=self.ctx.manifest.config_dir,
            service_user=self.ctx.manifest.service_user or "patchtrack",
            proxy_url=self.ctx.cfg.proxy_url,
            no_proxy=self.ctx.cfg.no_proxy,
        )
        base.copy_application(staging_cfg)
        user = self.ctx.manifest.service_user or "patchtrack"
        group = self.ctx.manifest.service_group or user
        base.run_command(
            ["chown", "-R", f"{user}:{group}", str(self.ctx.staging)]
        )
        os.symlink(
            str(self.ctx.manifest.env_file),
            str(self.ctx.staging / ".env"),
        )
        base.info(f"staged {self.ctx.source} in {self.ctx.staging}")

    def swap(self) -> None:
        base.step("Installing the new application tree")
        self.ctx.manifest.install_dir.rename(self.ctx.previous)
        self.swapped = True
        try:
            self.ctx.staging.rename(self.ctx.manifest.install_dir)
        except OSError:
            self.ctx.previous.rename(self.ctx.manifest.install_dir)
            self.swapped = False
            raise
        base.info(f"replaced {self.ctx.manifest.install_dir} (previous tree kept for rollback)")

    def set_version(self) -> None:
        base.step(f"Updating the version to {self.ctx.new_version}")
        replace_env_version(self.ctx.manifest.env_file, self.ctx.new_version)
        self.version_changed = True
        base.info(f"PATCHTRACK_VERSION={self.ctx.new_version}")

    def build_image(self) -> None:
        base.step("Building the new image")
        self._compose("build")
        image = f"patchtrack:{self.ctx.new_version}"
        if not base.image_exists(self.ctx.manifest.runtime, image):
            raise UpgradeError(f"image {image} was not created")
        base.info(f"image {image} is ready")

    def _postgres_ready(self) -> bool:
        return self._compose(
            "exec", "-T", "postgres", "pg_isready",
            "-U", "patchtrack", "-d", "patchtrack",
            check=False, capture=True,
        ).returncode == 0

    def stop_services(self) -> None:
        base.step("Stopping the application services")
        self.services_stopped = True
        if self.unit_owned:
            # Stopping the unit also stops PostgreSQL, so bring the database
            # back before migrations run. The unit is started again, not
            # Compose directly, so systemd's active state stays accurate.
            result = base.run_command(
                ["systemctl", "stop", base.SYSTEMD_SERVICE_NAME],
                check=False, capture=True,
            )
            if result.returncode != 0:
                raise UpgradeError(
                    "could not stop the PatchTrack systemd service: "
                    + (result.stderr or result.stdout or "").strip()
                )
            result = self._compose(
                "up", "-d", "postgres", check=False, capture=True
            )
            if result.returncode != 0:
                raise UpgradeError(
                    "could not restart PostgreSQL for migrations: "
                    + (result.stderr or result.stdout or "").strip()
                )
            base.wait_for(
                self._postgres_ready, timeout=120, interval=3,
                label="PostgreSQL",
            )
            base.info("systemd service stopped; PostgreSQL is ready for migrations")
            return

        result = self._compose(
            "stop", "gateway", "identity", "inventory", "compliance", "reporting",
            check=False, capture=True,
        )
        if result.returncode != 0:
            raise UpgradeError(
                "could not stop the application services: "
                + (result.stderr or result.stdout or "").strip()
            )
        base.info("application services stopped; PostgreSQL remains running")

    def migrate(self) -> None:
        base.step("Applying database migrations")
        self.db_touched = True
        result = self._compose("run", "--rm", "migrate", check=False, capture=True)
        if result.returncode != 0:
            raise UpgradeError(
                "migrations failed: "
                + ((result.stderr or result.stdout or "").strip().splitlines()[-1:]
                   or ["unknown error"])[0]
            )
        base.info("migrations applied")

    def start(self) -> None:
        base.step("Starting the upgraded services")
        if self.unit_owned:
            result = base.run_command(
                ["systemctl", "start", base.SYSTEMD_SERVICE_NAME],
                check=False, capture=True,
            )
        else:
            result = self._compose("up", "-d", check=False, capture=True)
        if result.returncode != 0:
            raise UpgradeError(
                "could not start the services: "
                + (result.stderr or result.stdout or "").strip()
            )
        wait_for_ready(self.ctx.probe, "the upgraded services")
        base.info("all four services are ready")

    def record_manifest(self) -> None:
        base.step("Recording the upgraded version")
        data = dict(self.ctx.data)
        data["previous_version"] = self.ctx.current_version
        data["version"] = self.ctx.new_version
        data["image"] = f"patchtrack:{self.ctx.new_version}"
        data["upgraded_at"] = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
        data["verified"] = True
        write_manifest(self.ctx.manifest.path, data)
        base.info(f"install.json now records {self.ctx.new_version}")

    def verify(self) -> None:
        base.step("Verifying the upgrade")
        open_url = opener()
        status, health = get_json(open_url, self.ctx.probe + "/healthz")
        if status != 200 or health.get("status") != "ok":
            raise UpgradeError("the gateway health endpoint did not report ok")
        request = urllib.request.Request(self.ctx.probe + "/login", method="GET")
        with open_url.open(request, timeout=10) as response:
            if response.status != 200:
                raise UpgradeError("the sign-in page is not available")
        result = self._compose(
            "exec", "-T", "postgres", "psql", "-U", "patchtrack",
            "-d", "patchtrack", "-tAc", "SELECT count(*) FROM shared.schema_migrations",
            check=False, capture=True,
        )
        if result.returncode != 0:
            raise UpgradeError("the migrations table is not readable")
        base.info(
            "HTTPS, the four services, the sign-in page and the migration "
            "table are verified"
        )

    def run(self) -> None:
        self.stage()
        self.swap()
        self.set_version()
        self.build_image()
        self.stop_services()
        self.migrate()
        self.start()
        self.verify()
        self.record_manifest()
        self.complete()

    def complete(self) -> None:
        try:
            base.remove_tree(self.ctx.previous)
        except (OSError, base.InstallerError) as exc:
            base.warn(f"could not remove the previous tree {self.ctx.previous}: {exc}")
        else:
            base.info("previous application tree removed (backup retained)")

    # -- rollback

    def rollback(self) -> bool:
        base.step("Restoring the previous installation")
        failures: List[str] = []
        env = base.command_environment(self.ctx.cfg)

        if not (self.swapped or self.version_changed
                or self.services_stopped or self.db_touched):
            try:
                base.remove_tree(self.ctx.staging)
            except (OSError, base.InstallerError) as exc:
                failures.append(f"could not remove the staging tree: {exc}")
            if failures:
                for failure in failures:
                    base.error(failure)
                return False
            base.info("No change had been made; the running version was left untouched.")
            return True

        if self.services_stopped or self.swapped or self.db_touched:
            stop = self._compose(
                "stop", "gateway", "identity", "inventory", "compliance", "reporting",
                check=False, capture=True,
            )
            if stop.returncode != 0:
                failures.append(
                    "could not stop the new services: "
                    + (stop.stderr or stop.stdout or "").strip()
                )

        if self.version_changed:
            try:
                shutil.copy2(self.backup.config / ".env", self.ctx.manifest.env_file)
                base.info("restored the previous .env")
            except OSError as exc:
                failures.append(f"could not restore .env: {exc}")

        if self.swapped:
            try:
                base.remove_tree(self.ctx.manifest.install_dir)
                if self.ctx.previous.exists():
                    self.ctx.previous.rename(self.ctx.manifest.install_dir)
                else:
                    shutil.copytree(
                        str(self.backup.app), str(self.ctx.manifest.install_dir),
                        symlinks=True, copy_function=shutil.copy2,
                    )
                base.info("restored the previous application tree")
            except (OSError, base.InstallerError) as exc:
                failures.append(f"could not restore the application tree: {exc}")

        try:
            base.remove_tree(self.ctx.staging)
        except (OSError, base.InstallerError):
            pass

        if self.ctx.manifest.path.exists():
            try:
                shutil.copy2(self.backup.config / "install.json", self.ctx.manifest.path)
            except OSError as exc:
                failures.append(f"could not restore install.json: {exc}")

        if self.db_touched:
            base.info("recreating the database from the pre-upgrade dump")
            # All application containers were stopped above. Terminate any
            # lingering backend so the failed database can be renamed aside.
            self._psql(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = 'patchtrack' AND pid <> pg_backend_pid()"
            )
            failed_name = (
                "patchtrack_failed_upgrade_"
                + _dt.datetime.now().strftime("%Y%m%d%H%M%S")
            )
            rename = self._psql(
                f'ALTER DATABASE patchtrack RENAME TO "{failed_name}"'
            )
            created = None
            if rename.returncode == 0:
                created = self._psql("CREATE DATABASE patchtrack OWNER patchtrack")
                if created.returncode != 0:
                    # Do not leave the deployment without a database: put the
                    # original back under its real name before reporting.
                    self._psql(
                        f'ALTER DATABASE "{failed_name}" RENAME TO patchtrack'
                    )
                    failures.append(
                        "could not recreate the database: "
                        + (created.stderr or created.stdout or "").strip()
                    )
            else:
                failures.append(
                    "could not rename the failed database aside: "
                    + (rename.stderr or rename.stdout or "").strip()
                )

            if created is not None and created.returncode == 0:
                with open(self.backup.database, "rb") as dump:
                    result = run_stream(
                        compose_argv(
                            self.ctx.manifest, "exec", "-T", "postgres", "pg_restore",
                            "-U", "patchtrack", "-d", "patchtrack",
                            "--no-owner", "--no-privileges", "--single-transaction",
                        ),
                        self.ctx.manifest.install_dir,
                        stdin=dump,
                        env=env,
                    )
                if result.returncode != 0:
                    detail = (result.stderr or b"").decode("utf-8", "replace").strip()
                    failures.append(
                        "database restore failed; restore "
                        f"{self.backup.database} manually (the failed database "
                        f"is kept as {failed_name}): {detail}"
                    )
                else:
                    base.info(
                        "database restored from the pre-upgrade dump; the failed "
                        f"database is kept as {failed_name}"
                    )

        if not base.image_exists(self.ctx.manifest.runtime, self.ctx.manifest.image):
            base.info(f"rebuilding the previous image {self.ctx.manifest.image}")
            build = self._compose("build", check=False, capture=True)
            if build.returncode != 0:
                failures.append(
                    "could not rebuild the previous image: "
                    + (build.stderr or build.stdout or "").strip()
                )

        if self.services_stopped or self.db_touched:
            if self.unit_owned:
                start = base.run_command(
                    ["systemctl", "start", base.SYSTEMD_SERVICE_NAME],
                    check=False, capture=True,
                )
            else:
                start = self._compose("up", "-d", check=False, capture=True)
            if start.returncode != 0:
                failures.append(
                    "could not start the previous services: "
                    + (start.stderr or start.stdout or "").strip()
                )
            else:
                try:
                    wait_for_ready(self.ctx.probe, "the restored services")
                    base.info("the previous version is running and ready")
                except base.InstallerError as exc:
                    failures.append(str(exc))

        self.failures.extend(failures)
        if failures:
            base.error("Rollback could not restore everything:")
            for failure in failures:
                print(f"    - {failure}", file=sys.stderr)
            return False
        base.info("Rollback completed; the previous version is running.")
        return True


# ------------------------------------------------------------------------ flow

def summary(ctx: Context, state: RuntimeState) -> None:
    print()
    print(base.colour("Upgrade summary", "1;1"))
    print(f"    From version          : {ctx.current_version}")
    print(f"    To version            : {ctx.new_version}")
    print(f"    New release           : {ctx.source}")
    print(f"    Application directory : {ctx.manifest.install_dir}")
    print(f"    Configuration         : {ctx.manifest.config_dir}")
    print(f"    Runtime               : {ctx.manifest.runtime}")
    print(f"    Backup                : {ctx.backup_root}")
    print("    Services will restart ; a short interruption is expected.")
    print("    On failure the previous tree, .env, manifest and database are restored.")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="upgrade.sh",
        description=(
            "Guided PatchTrack upgrade: preflight, backup, staged replacement, "
            "migrations, verification and automatic rollback."
        ),
    )
    parser.add_argument(
        "--version", default=None,
        help=(
            "target version, e.g. 3.1.0 (default: PATCHTRACK_UPGRADE_VERSION, "
            "then PATCHTRACK_VERSION)"
        ),
    )
    parser.add_argument(
        "--check", action="store_true",
        help="run the prechecks and print the plan without changing anything",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="non-interactive; do not ask for confirmation",
    )
    parser.add_argument(
        "--config-dir", default=str(base.DEFAULT_CONFIG_DIR),
        help="configuration directory containing install.json (default: /etc/patchtrack)",
    )
    parser.add_argument(
        "--source-dir", default=str(base.REPO_ROOT),
        help="unpacked new release directory (default: this script's directory)",
    )
    parser.add_argument(
        "--backup-dir", default=str(DEFAULT_BACKUP_ROOT),
        help=f"where to keep the pre-upgrade backup (default: {DEFAULT_BACKUP_ROOT})",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if not args.version:
        args.version = (
            os.environ.get("PATCHTRACK_UPGRADE_VERSION")
            or os.environ.get("PATCHTRACK_VERSION", "")
        )

    try:
        ctx = build_context(args)
    except (UpgradeError, manifest_reader.UninstallError) as exc:
        base.error(str(exc))
        return 1

    checks, state = collect_preflight(ctx)
    print(base.colour("PatchTrack upgrade prechecks", "1;1"))
    failures, warnings = base.print_checks(checks)
    if failures:
        print()
        base.error(f"{failures} required precheck(s) failed; nothing has been changed.")
        return 1

    summary(ctx, state)
    if args.check:
        print()
        if warnings:
            base.warn(f"{warnings} warning(s); review the notes above.")
        else:
            base.info("All required prechecks passed; nothing has been changed.")
        return 0

    if not args.yes:
        try:
            if not _ask_yes_no("Proceed with the upgrade?", False):
                base.info("Upgrade cancelled; nothing has been changed.")
                return 0
        except UserCancelled as exc:
            base.error(str(exc))
            return 2

    try:
        backup = create_backup(ctx)
    except (UpgradeError, OSError) as exc:
        base.error(str(exc))
        base.info("Nothing has been changed.")
        return 1

    upgrade = Upgrade(ctx, backup)

    def _terminate(signum, frame):  # noqa: ANN001
        raise KeyboardInterrupt

    old_term = base.signal.signal(base.signal.SIGTERM, _terminate)
    try:
        upgrade.run()
    except KeyboardInterrupt:
        print()
        base.error("Upgrade interrupted.")
        upgrade.rollback()
        return 130
    except (UpgradeError, base.InstallerError, OSError) as exc:
        print()
        base.error(str(exc))
        upgrade.rollback()
        return 1
    except Exception as exc:  # noqa: BLE001 - any failure must trigger rollback
        print()
        base.error(f"unexpected upgrade failure: {exc}")
        upgrade.rollback()
        return 1
    finally:
        base.signal.signal(base.signal.SIGTERM, old_term)

    print()
    print(base.colour("Upgrade complete.", "1;32"))
    print(f"    Version: {ctx.current_version} → {ctx.new_version}")
    print(f"    Backup:  {backup.root}")
    print("    The previous image was kept on the host for rollback.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
