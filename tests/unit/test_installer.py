"""The guided installer checks inputs before it changes the host.

These tests exercise the pure validation, configuration rendering and rollback
ordering. They do not install packages, start containers or touch the host;
the destructive path belongs to the installer's own preflight and is covered by
``install.sh --check`` on a target host.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "patchtrack_installer", ROOT / "scripts" / "patchtrack_install.py"
)
assert SPEC and SPEC.loader
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)


def test_installer_validates_the_gateway_bind_address():
    assert installer.validate_bind("0.0.0.0") is None
    assert installer.validate_bind("192.0.2.10") is None
    assert installer.validate_bind("999.0.0.1") is not None
    assert installer.validate_bind("patchtrack.example.com") is not None
    assert installer.validate_bind("::") is not None


def test_installer_validates_the_gateway_port():
    assert installer.validate_port("8005") is None
    assert installer.validate_port("1") is None
    assert installer.validate_port("0") is not None
    assert installer.validate_port("65536") is not None
    assert installer.validate_port("http") is not None


def test_installer_validates_the_certificate_name_and_accounts():
    assert installer.validate_hostname("patchtrack.example.com") is None
    assert installer.validate_hostname("patchtrack") is None
    assert installer.validate_hostname("bad_name.example.com") is not None
    assert installer.validate_service_user("patchtrack") is None
    assert installer.validate_service_user("PatchTrack") is not None
    assert installer.validate_admin_username("admin.user") is None
    assert installer.validate_admin_username("ab") is not None


def test_installer_rejects_email_domains_the_identity_service_refuses():
    assert installer.validate_email("") is None
    assert installer.validate_email("admin@example.com") is None
    assert installer.validate_email("admin@corp.example") is None
    for value in (
        "admin@example.invalid",
        "admin@host.local",
        "admin@example.test",
        "admin@example.localhost",
        "admin@node.onion",
        "admin@1.2.3.4.arpa",
    ):
        assert installer.validate_email(value) is not None, value


def test_installer_refuses_to_install_over_its_own_source_tree():
    problem = installer.validate_install_path(str(ROOT), "install directory")
    assert problem is not None
    assert "source directory" in problem


def test_installer_refuses_a_non_empty_target_directory(tmp_path):
    install_dir = tmp_path / "opt" / "patchtrack"
    install_dir.mkdir(parents=True)
    (install_dir / "keep.txt").write_text("user data")
    config_dir = tmp_path / "etc" / "patchtrack"

    cfg = installer.Config(install_dir=install_dir, config_dir=config_dir)
    checks = installer.target_path_checks(cfg)

    failed = [check for check in checks if not check.ok]
    assert any("not empty" in check.detail for check in failed)


def test_generated_environment_contains_no_bootstrap_secret():
    cfg = installer.Config()
    text = installer.build_env(cfg, "patchtrack")

    assert "PATCHTRACK_JWT_SECRET=\n" in text
    assert "PATCHTRACK_SERVICE_TOKEN=\n" in text
    assert "PATCHTRACK_DATABASE_URL=\n" in text
    assert "POSTGRES_PASSWORD=\n" in text
    assert "CHANGE_ME" not in text
    assert "PATCHTRACK_BIND_ADDRESS=0.0.0.0" in text
    assert "PATCHTRACK_TLS_ENABLED=true" in text


def test_the_systemd_service_owns_the_whole_compose_stack(tmp_path):
    install_dir = tmp_path / "opt" / "patchtrack"

    docker = installer.compose_service_unit(install_dir, "docker")
    assert "Type=simple" in docker
    assert f"WorkingDirectory={install_dir}" in docker
    assert f"ConditionPathExists={install_dir}/docker-compose.yml" in docker
    assert "Requires=docker.service" in docker
    assert "/usr/bin/docker compose -f docker-compose.yml " \
           "-f docker-compose.secrets.yml up --remove-orphans" in docker
    assert "/usr/bin/docker compose -f docker-compose.yml " \
           "-f docker-compose.secrets.yml stop" in docker
    assert "TimeoutStopSec=180" in docker
    assert "WantedBy=multi-user.target" in docker
    assert installer.SYSTEMD_UNIT_MARKER in docker

    podman = installer.compose_service_unit(install_dir, "podman")
    assert "/usr/bin/podman-compose" in podman
    assert "docker.service" not in podman

    assert installer.systemd_unit_is_installer_owned(docker, install_dir)
    assert not installer.systemd_unit_is_installer_owned(
        docker, tmp_path / "opt" / "elsewhere"
    )
    assert not installer.systemd_unit_is_installer_owned(
        "[Unit]\nDescription=Something else\n", install_dir
    )


def test_installer_validates_the_proxy_and_bypass_list():
    assert installer.validate_proxy("") is None
    assert installer.validate_proxy("http://proxy.example.com:3128") is None
    assert installer.validate_proxy("https://user:pass@proxy.example.com") is None
    assert installer.validate_proxy("socks5://proxy.example.com:1080") is not None
    assert installer.validate_proxy("http://") is not None
    assert installer.validate_proxy("http://proxy.example.com/path") is not None
    assert installer.validate_proxy("http://proxy.example.com:0") is not None
    assert installer.validate_proxy("http://proxy example.com") is not None
    assert installer.validate_proxy('http://proxy/"quoted"') is not None

    assert installer.validate_no_proxy("") is None
    assert installer.validate_no_proxy("localhost,127.0.0.1,*.example.com") is None
    assert installer.validate_no_proxy("localhost,,postgres") is not None
    assert installer.validate_no_proxy("local host") is not None


def test_proxy_display_never_shows_credentials():
    assert installer.display_proxy("") == "direct access"
    assert installer.display_proxy("http://proxy.example.com:3128") == (
        "http://proxy.example.com:3128"
    )
    hidden = installer.display_proxy("http://user:secret@proxy.example.com:8080")
    assert hidden == "http://***@proxy.example.com:8080"
    assert "secret" not in hidden


def test_generated_environment_applies_the_proxy_to_the_services():
    cfg = installer.Config(
        proxy_url="http://user:secret@proxy.example.com:3128",
        no_proxy="localhost,127.0.0.1,postgres",
    )
    text = installer.build_env(cfg, "patchtrack")

    assert "HTTP_PROXY='http://user:secret@proxy.example.com:3128'" in text
    assert "HTTPS_PROXY='http://user:secret@proxy.example.com:3128'" in text
    assert "NO_PROXY='localhost,127.0.0.1,postgres'" in text
    # Bootstrap secrets remain file-backed.
    assert "PATCHTRACK_JWT_SECRET=\n" in text
    assert "PATCHTRACK_DATABASE_URL=\n" in text

    assert "HTTP_PROXY" not in installer.build_env(installer.Config(), "patchtrack")


def test_docker_proxy_unit_escapes_systemd_expansion():
    cfg = installer.Config(proxy_url="http://user:p%41ss@proxy.example.com:3128")
    unit = installer.docker_proxy_unit(cfg)

    assert unit.startswith("[Service]\nEnvironment=")
    assert "HTTP_PROXY=http://user:p%%41ss@proxy.example.com:3128" in unit
    assert '"NO_PROXY=' in unit


def test_command_environment_is_unchanged_without_a_proxy(monkeypatch):
    monkeypatch.setenv("PATCHTRACK_INSTALL_TEST", "kept")
    assert installer.command_environment(installer.Config()) is None

    cfg = installer.Config(proxy_url="http://proxy.example.com:3128")
    environment = installer.command_environment(cfg)
    assert environment["PATCHTRACK_INSTALL_TEST"] == "kept"
    assert environment["HTTPS_PROXY"] == "http://proxy.example.com:3128"
    assert environment["http_proxy"] == "http://proxy.example.com:3128"
    assert environment["DOCKER_BUILDKIT"] == "1"


def test_probe_uses_loopback_when_the_gateway_is_published_on_all_interfaces():
    cfg = installer.Config(bind_address="0.0.0.0", gateway_port=8443)
    assert cfg.probe_host == "127.0.0.1"
    assert cfg.base_url == "https://127.0.0.1:8443"

    cfg = installer.Config(bind_address="192.0.2.10", gateway_port=8443)
    assert cfg.probe_host == "192.0.2.10"


def test_rollback_runs_actions_in_reverse_order():
    class Recording(installer.RollbackAction):
        def __init__(self, label, calls):
            self.label = label
            self.calls = calls

        def rollback(self):
            self.calls.append(self.label)

    calls = []
    subject = installer.Installer(
        installer.Config(), installer.RuntimePlan("docker", install_packages=False)
    )
    subject.actions = [
        Recording("directory", calls),
        Recording("files", calls),
        Recording("compose", calls),
    ]

    assert subject.rollback() is True
    assert calls == ["compose", "files", "directory"]


def test_rollback_stops_before_removing_protected_paths():
    with pytest.raises(installer.InstallerError):
        installer.remove_tree(Path("/etc"))


def test_rollback_removes_container_stores_the_installation_created(monkeypatch):
    import types

    monkeypatch.setattr(
        installer, "run_command",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(installer, "wait_for", lambda *a, **k: None)

    docker = installer.Installer(
        installer.Config(), installer.RuntimePlan("docker", install_packages=False)
    )
    monkeypatch.setattr(docker, "configure_docker_proxy", lambda: None)
    docker._data_existed = {path: False for path in docker._data_dirs}
    docker.start_runtime()

    removed = [
        action.path for action in docker.actions
        if isinstance(action, installer.RemoveTree)
    ]
    assert removed == [Path("/var/lib/docker"), Path("/var/lib/containerd")]

    podman = installer.Installer(
        installer.Config(), installer.RuntimePlan("podman", install_packages=False)
    )
    podman._data_existed = {path: False for path in podman._data_dirs}
    podman.start_runtime()

    removed = [
        action.path for action in podman.actions
        if isinstance(action, installer.RemoveTree)
    ]
    assert removed == [Path("/var/lib/containers")]


def test_pre_existing_container_store_is_not_removed_on_rollback(monkeypatch):
    import types

    monkeypatch.setattr(
        installer, "run_command",
        lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(installer, "wait_for", lambda *a, **k: None)
    subject = installer.Installer(
        installer.Config(), installer.RuntimePlan("docker", install_packages=False)
    )
    monkeypatch.setattr(subject, "configure_docker_proxy", lambda: None)
    subject._data_existed = {path: True for path in subject._data_dirs}

    subject.start_runtime()

    assert not any(isinstance(action, installer.RemoveTree) for action in subject.actions)


class _FakeGateway:
    """A minimal HTTPS stand-in for the gateway's bootstrap endpoints."""

    def __init__(self, me_username="admin"):
        self.me_username = me_username
        self.seen = {}

    def handler(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, code, body, headers=()):
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/session/status":
                    if "patchtrack_session=" in (self.headers.get("Cookie") or ""):
                        self._send(200, {"authenticated": True, "needs_admin": False})
                    else:
                        self._send(200, {"authenticated": False, "needs_admin": True})
                elif self.path == "/api/me":
                    self._send(200, {
                        "username": parent.me_username,
                        "must_change_password": True,
                    })
                else:
                    self._send(404, {"detail": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                parent.seen["payload"] = json.loads(self.rfile.read(length))
                self._send(200, {"status": "ok"}, [
                    ("Set-Cookie", "patchtrack_session=test; Secure; HttpOnly; Path=/"),
                ])

        return Handler


@pytest.fixture
def fake_gateway(tmp_path):
    import ssl
    import subprocess
    import threading
    from http.server import ThreadingHTTPServer

    if not shutil.which("openssl"):
        pytest.skip("openssl is required to generate the test certificate")
    cert = tmp_path / "gateway.crt"
    key = tmp_path / "gateway.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-subj", "/CN=127.0.0.1",
        ],
        check=True, capture_output=True,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(cert), str(key))

    servers = []

    def start(me_username="admin"):
        gateway = _FakeGateway(me_username)
        server = ThreadingHTTPServer(("127.0.0.1", 0), gateway.handler())
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append((server, thread))
        cfg = installer.Config(
            bind_address="127.0.0.1",
            gateway_port=server.server_address[1],
            admin_username="admin",
            admin_password="a-sufficiently-long-password",
            create_admin=True,
        )
        return gateway, cfg

    yield start

    for server, thread in servers:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_installer_verifies_the_new_administrator_session(fake_gateway):
    gateway, cfg = fake_gateway()
    subject = installer.Installer(cfg, installer.RuntimePlan("docker", install_packages=False))

    subject.create_administrator()

    assert subject.admin_created is True
    assert gateway.seen["payload"]["username"] == "admin"
    assert gateway.seen["payload"]["password"] == "a-sufficiently-long-password"


def test_installer_fails_when_the_verified_session_is_not_the_new_admin(fake_gateway):
    _, cfg = fake_gateway(me_username="someone-else")
    subject = installer.Installer(cfg, installer.RuntimePlan("docker", install_packages=False))

    with pytest.raises(installer.InstallerError, match="does not belong"):
        subject.create_administrator()
    assert subject.admin_created is False


def test_installer_help_is_safe_to_run_without_root(tmp_path):
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "patchtrack_install.py"), "--help"],
        capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0
    assert "--check" in result.stdout
    assert "--yes" in result.stdout


def test_installer_accepts_airgap_options():
    args = installer.parse_args(
        ["--airgap", "--bundle-dir", "/srv/bundles/patchtrack-3.0.0", "--check"]
    )

    assert args.airgap is True
    assert args.bundle_dir == "/srv/bundles/patchtrack-3.0.0"
    assert installer.validate_bundle_dir("/srv/bundles/patchtrack-3.0.0") is None
    assert installer.validate_bundle_dir("bundles/patchtrack") is not None


def test_airgap_bundle_checksum_verification_detects_tampering(tmp_path):
    import hashlib

    archive = tmp_path / "postgres-17.tar"
    archive.write_bytes(b"postgres image archive")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (tmp_path / installer.AIRGAP_SUMS).write_text(
        f"{digest}  {archive.name}\n"
    )

    ok, detail = installer.verify_bundle(tmp_path)
    assert ok is True
    assert "1 file(s) verified" in detail

    archive.write_bytes(b"tampered")
    ok, detail = installer.verify_bundle(tmp_path)
    assert ok is False
    assert archive.name in detail


def test_airgap_bundle_checksum_verification_requires_the_sums_file(tmp_path):
    ok, detail = installer.verify_bundle(tmp_path)

    assert ok is False
    assert installer.AIRGAP_SUMS in detail


def test_airgap_preflight_checks_the_bundle_and_skips_registry_probes(tmp_path):
    import hashlib

    version = installer.Config().version
    archives = installer.bundle_archives(version)
    for key in ("app", "base", "postgres"):
        (tmp_path / archives[key]).write_bytes(key.encode())
    sums = []
    for title in ("app", "base", "postgres"):
        name = archives[title]
        digest = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        sums.append(f"{digest}  {name}")
    (tmp_path / installer.AIRGAP_SUMS).write_text("\n".join(sums) + "\n")

    cfg = installer.Config(airgap=True, bundle_dir=tmp_path)
    checks = installer.bundle_preflight_checks(
        cfg, installer.RuntimePlan("podman", False), runtime_ready=False
    )
    by_name = {check.name: check for check in checks}

    assert by_name["air-gap mode"].ok
    assert by_name["bundle checksums"].ok
    assert by_name["application image archive"].ok
    assert by_name["PostgreSQL image archive"].ok
    assert not any("registry" in name.lower() for name in by_name)
    assert all(check.ok for check in checks if check.required)


def test_airgap_preflight_rejects_a_bundle_missing_a_required_archive(tmp_path):
    (tmp_path / installer.AIRGAP_SUMS).write_text("")
    cfg = installer.Config(airgap=True, bundle_dir=tmp_path)

    checks = installer.bundle_preflight_checks(
        cfg, installer.RuntimePlan("podman", False), runtime_ready=False
    )
    by_name = {check.name: check for check in checks}

    assert by_name["application image archive"].ok is False
    assert by_name["PostgreSQL image archive"].ok is False
    assert all(
        check.remediation for check in checks if not check.ok or check.warning
    )


def test_preflight_output_includes_remediation_only_for_problems(capsys):
    checks = [
        installer.Check(
            "memory", False, "1 GiB",
            remediation="Allocate at least 4 GiB.",
        ),
        installer.Check(
            "container image registry", True, "not reachable",
            required=False, warning=True,
            remediation="Use a proxy or an air-gap bundle.",
        ),
        installer.Check(
            "CPU", True, "4 vCPU", remediation="This line should not be printed.",
        ),
    ]

    failures, warnings = installer.print_checks(checks)
    output = capsys.readouterr().out

    assert (failures, warnings) == (1, 1)
    assert "how to fix: Allocate at least 4 GiB." in output
    assert "how to fix: Use a proxy or an air-gap bundle." in output
    assert "This line should not be printed." not in output


def test_airgap_installer_loads_images_and_skips_the_build(tmp_path, monkeypatch):
    import subprocess

    version = installer.Config().version
    cfg = installer.Config(airgap=True, bundle_dir=tmp_path)
    subject = installer.Installer(cfg, installer.RuntimePlan("podman", False))
    state = set()

    def fake_image_exists(runtime, image):
        if image == cfg.image:
            return f"localhost/patchtrack:{version}" in state
        return image in state

    def fake_load(archive):
        state.add({
            f"patchtrack-app-{version}.tar":
                f"docker.io/library/patchtrack:{version}",
            installer.AIRGAP_POSTGRES_ARCHIVE: installer.AIRGAP_POSTGRES_IMAGE,
        }[archive.name])

    def fake_run(args, **kwargs):
        if list(args[:2]) == ["podman", "tag"]:
            state.add(args[3])
        elif list(args[:3]) == ["podman", "image", "rm"]:
            state.discard(args[3])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subject, "load_archive", fake_load)
    monkeypatch.setattr(installer, "image_exists", fake_image_exists)
    monkeypatch.setattr(installer, "run_command", fake_run)

    subject.prepare_images()

    assert subject.prebuilt_image is True
    assert subject.image_loaded is True
    assert f"localhost/patchtrack:{version}" in state
    assert f"docker.io/library/patchtrack:{version}" not in state
    assert installer.AIRGAP_POSTGRES_IMAGE in state


def test_airgap_bundle_must_list_its_required_archives(tmp_path):
    import hashlib

    archive = tmp_path / "patchtrack-app-3.0.0.tar"
    archive.write_bytes(b"application image")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (tmp_path / installer.AIRGAP_SUMS).write_text(
        f"{digest}  {archive.name}\n"
    )

    ok, detail = installer.verify_bundle(
        tmp_path, required=(archive.name, installer.AIRGAP_POSTGRES_ARCHIVE)
    )

    assert ok is False
    assert installer.AIRGAP_POSTGRES_ARCHIVE in detail


def test_airgap_bundle_tolerates_a_removed_optional_archive(tmp_path):
    import hashlib

    names = ("patchtrack-app-3.0.0.tar", installer.AIRGAP_POSTGRES_ARCHIVE)
    sums = []
    for name in names:
        (tmp_path / name).write_bytes(name.encode())
        digest = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        sums.append(f"{digest}  {name}")
    optional = "patchtrack-wheels.tar"
    (tmp_path / optional).write_bytes(b"optional")
    digest = hashlib.sha256((tmp_path / optional).read_bytes()).hexdigest()
    sums.append(f"{digest}  {optional}")
    (tmp_path / installer.AIRGAP_SUMS).write_text("\n".join(sums) + "\n")
    (tmp_path / optional).unlink()

    ok, detail = installer.verify_bundle(tmp_path, required=names)

    assert ok is True
    assert "2 file(s) verified" in detail


def test_airgap_bundle_must_not_overlap_the_install_directories(tmp_path):
    install_dir = tmp_path / "opt" / "patchtrack"
    config_dir = tmp_path / "etc" / "patchtrack"
    bundle = install_dir / "bundle"
    cfg = installer.Config(
        install_dir=install_dir,
        config_dir=config_dir,
        airgap=True,
        bundle_dir=bundle,
    )

    problems = installer.validate_config(cfg)

    assert any("bundle directory must not overlap" in problem for problem in problems)


def test_installer_accepts_sample_data_options():
    args = installer.parse_args(
        ["--yes", "--sample-assets", "250", "--sample-years", "3"]
    )
    assert args.sample_data is False
    assert args.sample_assets == 250
    assert args.sample_years == 3

    args = installer.parse_args(["--yes", "--sample-data"])
    assert args.sample_data is True


def test_installer_reads_sample_data_from_the_environment(monkeypatch):
    monkeypatch.setenv("PATCHTRACK_INSTALL_SAMPLE_DATA", "yes")
    monkeypatch.setenv("PATCHTRACK_INSTALL_SAMPLE_ASSETS", "500")
    monkeypatch.setenv("PATCHTRACK_INSTALL_SAMPLE_YEARS", "2")

    cfg = installer.config_from_env(installer.parse_args(["--yes"]))

    assert cfg.sample_data is True
    assert cfg.sample_assets == 500
    assert cfg.sample_years == 2


def test_sample_data_options_imply_sample_data_and_are_validated(monkeypatch):
    monkeypatch.delenv("PATCHTRACK_INSTALL_SAMPLE_DATA", raising=False)
    cfg = installer.config_from_env(
        installer.parse_args(["--yes", "--sample-assets", "25"])
    )
    assert cfg.sample_data is True
    assert installer.validate_config(cfg) == []

    cfg.sample_assets = 0
    assert any("sample server count" in item for item in installer.validate_config(cfg))
    cfg.sample_assets = installer.MAX_SAMPLE_ASSETS + 1
    assert any("sample server count" in item for item in installer.validate_config(cfg))

    cfg = installer.Config(sample_data=True, sample_years=0)
    assert any("sample history years" in item for item in installer.validate_config(cfg))


def test_sample_data_command_mounts_secrets_and_scripts_read_only(tmp_path):
    cfg = installer.Config(
        install_dir=tmp_path / "opt" / "patchtrack",
        config_dir=tmp_path / "etc" / "patchtrack",
        sample_data=True,
    )

    command = installer.sample_data_command(
        cfg, "docker", "seed-demo.py", "--assets", "10", "--years", "1"
    )

    assert command[:4] == ["docker", "run", "--rm", "--network"]
    assert "patchtrack_default" in command
    assert f"{cfg.secrets_dir / 'database_url'}:/run/secrets/database_url:ro,z" in command
    assert f"{cfg.secrets_dir / 'jwt_secret'}:/run/secrets/jwt_secret:ro,z" in command
    assert (
        f"{cfg.install_dir / 'scripts' / 'seed-demo.py'}"
        ":/opt/patchtrack/scripts/seed-demo.py:ro,z"
    ) in command
    assert "PATCHTRACK_DATABASE_URL_FILE=/run/secrets/database_url" in command
    assert "PATCHTRACK_JWT_SECRET_FILE=/run/secrets/jwt_secret" in command
    assert command[-6:] == [
        "python", "/opt/patchtrack/scripts/seed-demo.py", "--assets", "10", "--years", "1"
    ]
    assert cfg.admin_password not in command
