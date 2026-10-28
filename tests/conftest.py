"""Shared pytest configuration.

Service packages are imported from the source tree rather than installed, which
mirrors how the container and the venv run them (PYTHONPATH =
services/common:services).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

for relative in ("services/common", "services"):
    path = str(ROOT / relative)
    if path not in sys.path:
        sys.path.insert(0, path)

# The common Settings object requires these at import time. Tests that touch a
# database override the URL; unit tests only need the object to construct.
os.environ.setdefault("PATCHTRACK_JWT_SECRET", "test-only-jwt-secret-not-for-production")
os.environ.setdefault("PATCHTRACK_DATABASE_URL", "postgresql+psycopg://invalid@/invalid")
# Tests drive jobs explicitly; a background loop would add database work and
# timing noise to unrelated assertions. Individual scheduler tests call the
# runner directly.
os.environ.setdefault("PATCHTRACK_SCHEDULER_ENABLED", "false")
