"""Everything the services import must be installed in the image.

The container image installs `deploy/container/requirements.txt` and nothing
else. A missing entry does not fail at build time — it fails when the service
starts, as it did when `email-validator` (needed by pydantic's `EmailStr`) was
absent from the list and every identity container exited on import.

The check reads the service sources rather than running them, and resolves
each third-party import to the distribution that provides it.
"""

from __future__ import annotations

import ast
import importlib.metadata
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
REQUIREMENTS = ROOT / "deploy" / "container" / "requirements.txt"

# First-party packages shipped inside the image.
FIRST_PARTY = {"app", "patchtrack_common"}

# Where importlib.metadata cannot be asked (package not installed here), map
# the import name to its distribution directly.
FALLBACK = {
    "jwt": "pyjwt",
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "argon2": "argon2-cffi",
    "pydantic_settings": "pydantic-settings",
    "email_validator": "email-validator",
    "psycopg": "psycopg",
    "sqlalchemy": "sqlalchemy",
    "reportlab": "reportlab",
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "httpx": "httpx",
    "jinja2": "jinja2",
}


def declared() -> set[str]:
    """Distribution names in the image's requirements, normalised."""
    names = set()
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.split("#")[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[<>=!~\[ ]", line, maxsplit=1)[0]
        names.add(name.lower().replace("_", "-"))
    return names


def imported_names() -> set[str]:
    names: set[str] = set()
    for path in SERVICES.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def provides(module: str) -> str | None:
    """The distribution that supplies `module`, or None if not installed."""
    mapping = importlib.metadata.packages_distributions()
    candidates = mapping.get(module)
    if candidates:
        return candidates[0].lower().replace("_", "-")
    return FALLBACK.get(module)


def test_nothing_is_imported_that_the_image_does_not_install():
    missing = []
    for module in sorted(imported_names()):
        if module in sys.stdlib_module_names or module in FIRST_PARTY:
            continue
        distribution = provides(module)
        if distribution is None:
            pytest.skip(f"{module} is not installed in this environment")
        if distribution not in declared():
            missing.append(f"{module} (from {distribution})")
    assert not missing, (
        "these imports have no entry in deploy/container/requirements.txt, so "
        "the container would fail at import time: " + ", ".join(missing)
    )


def test_email_validator_is_declared():
    # The specific regression this file was added for: pydantic raises when
    # EmailStr is used and the package is absent, but nothing in the source
    # imports email_validator directly — so the import scan above cannot see
    # it. The dependency is implicit in using EmailStr.
    uses_email_str = any(
        "EmailStr" in path.read_text() for path in SERVICES.rglob("*.py")
    )
    assert uses_email_str, "the check below is stale; EmailStr is gone"
    assert "email-validator" in declared()
