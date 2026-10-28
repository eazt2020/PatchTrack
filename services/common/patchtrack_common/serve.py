"""Service launcher used by the container entrypoint and the systemd unit.

It starts uvicorn exactly as the documented CLI would, with one addition: when
the gateway is configured for TLS, it builds the listener context, generating a
self-signed certificate on first start if none exists. Running through this
module is what makes a certificate uploaded in Settings take effect on new
connections without a restart; a gateway started any other way still stores the
files and tells the administrator a restart is required.

The argument surface deliberately mirrors the uvicorn flags the deployment
artifacts already pass, so the unit and entrypoint stay readable.
"""

from __future__ import annotations

import argparse
import os
import sys

import uvicorn

from .config import get_settings
from . import tls


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="patchtrack_common.serve")
    parser.add_argument("app", help="ASGI application, e.g. app.main:app")
    parser.add_argument("--app-dir", default="")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--proxy-headers", action="store_true")
    parser.add_argument("--forwarded-allow-ips", default="")
    parser.add_argument("--service", default=os.environ.get("PATCHTRACK_SERVICE", ""))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _arguments(argv)
    if args.app_dir:
        sys.path.insert(0, args.app_dir)
    settings = get_settings()
    options: dict[str, object] = {
        "host": args.host,
        "port": args.port,
        "log_level": args.log_level,
        "server_header": False,
    }
    if args.proxy_headers:
        options["proxy_headers"] = True
        if args.forwarded_allow_ips:
            options["forwarded_allow_ips"] = args.forwarded_allow_ips

    context = None
    if args.service == "gateway" and settings.tls_enabled:
        context = tls.load_context(settings)
        # is_ssl drives uvicorn's "https" branding and its own keystore load;
        # config.load() runs before we substitute the shared context so the
        # listener uses the object that a later upload reloads in place.
        options["ssl_certfile"] = str(tls.cert_path(settings))
        options["ssl_keyfile"] = str(tls.key_path(settings))

    config = uvicorn.Config(args.app, **options)
    if context is not None:
        config.load()
        config.ssl = context
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
