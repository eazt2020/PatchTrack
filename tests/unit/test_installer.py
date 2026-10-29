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
    for key in ("app", "base", "postgres", "grafana"):
        (tmp_path / archives[key]).write_bytes(key.encode())
    sums = []
    for title in ("app", "base", "postgres", "grafana"):
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
    (tmp_path / installer.AIRGAP_GRAFANA_ARCHIVE).write_bytes(b"grafana")
    digest = hashlib.sha256(
        (tmp_path / installer.AIRGAP_GRAFANA_ARCHIVE).read_bytes()
    ).hexdigest()
    sums.append(f"{digest}  {installer.AIRGAP_GRAFANA_ARCHIVE}")
    (tmp_path / installer.AIRGAP_SUMS).write_text("\n".join(sums) + "\n")
    (tmp_path / installer.AIRGAP_GRAFANA_ARCHIVE).unlink()

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
