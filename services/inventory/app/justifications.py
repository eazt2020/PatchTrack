"""Justifying and verifying a failing compliance state.

The compliance engine decides state from evidence the collectors supply. It
must not be rewritten to make a report look better. An operator who knows why
an asset is non-compliant, stale or unknown records that reason, a remediation
plan and supporting evidence against the asset; the state itself stays exactly
what the data says.

Justifications are versioned. Posting a new one supersedes the active one, so
the statement that applied when a report was generated can still be produced.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from datetime import date

# Reasons are deliberately few. A free-text "other" would make the report
# impossible to summarise; the statement carries the detail.
REASONS: dict[str, str] = {
    "accepted_risk": "Accepted risk",
    "compensating_control": "Compensating control in place",
    "scheduled_remediation": "Remediation scheduled",
    "false_positive": "False positive / assessment error",
    "decommissioning": "Asset being decommissioned",
    "collection_gap": "Collection or authentication gap",
    "investigation": "Under investigation",
}

# Only these states need a justification. A compliant asset has nothing to
# explain; a missing state is treated as unknown by the compliance engine.
JUSTIFIABLE_STATES = {"non_compliant", "stale", "unknown"}

MAX_EVIDENCE_PER_JUSTIFICATION = 5
MAX_EVIDENCE_BYTES = 5 * 1024 * 1024
MAX_STATEMENT = 4000
MIN_STATEMENT = 20
MAX_PLAN = 2000
ALLOWED_EVIDENCE_TYPES = {
    "image/png", "image/jpeg", "application/pdf",
    "text/plain", "text/csv",
    # Screenshots and logs are the common evidence; Office documents are
    # allowed because change records often arrive as one.
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


@dataclass(frozen=True)
class Evidence:
    filename: str
    content_type: str
    content: bytes
    sha256: str

    @property
    def size(self) -> int:
        return len(self.content)


def effective_state(state: str | None) -> str:
    """The state the compliance engine reports; a missing row is unknown."""
    return state or "unknown"


def validate_reason(reason_code: str) -> str:
    code = (reason_code or "").strip()
    if code not in REASONS:
        raise ValueError("Choose a reason from the offered list")
    return code


def validate_statement(statement: str) -> str:
    value = (statement or "").strip()
    if len(value) < MIN_STATEMENT:
        raise ValueError(
            f"Explain in at least {MIN_STATEMENT} characters why the asset is in this state"
        )
    if len(value) > MAX_STATEMENT:
        raise ValueError(f"The explanation must be at most {MAX_STATEMENT} characters")
    return value


def validate_plan(plan: str | None) -> str:
    value = (plan or "").strip()
    if len(value) > MAX_PLAN:
        raise ValueError(f"The remediation plan must be at most {MAX_PLAN} characters")
    return value


def validate_target(target: date | None) -> date | None:
    # A past target is a legitimate statement that remediation is overdue. It
    # is not rejected, because hiding it would defeat the purpose.
    return target


def _decode(data: str) -> bytes:
    raw = (data or "").strip()
    if raw.startswith("data:"):
        _, _, raw = raw.partition(",")
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Evidence could not be decoded") from exc


def normalise_evidence(items: list[dict[str, str]]) -> list[Evidence]:
    """Validate a batch of base64 evidence payloads.

    The caller supplies already decoded objects from the request model; this
    function owns the size, count and type rules so the API, the tests and any
    future importer apply the same policy.
    """
    if len(items) > MAX_EVIDENCE_PER_JUSTIFICATION:
        raise ValueError(
            f"Attach at most {MAX_EVIDENCE_PER_JUSTIFICATION} evidence files"
        )
    evidence: list[Evidence] = []
    for item in items:
        filename = (item.get("filename") or "").strip()
        if not filename:
            raise ValueError("Each evidence file needs a name")
        if len(filename) > 255:
            raise ValueError("An evidence filename is too long")
        content_type = (item.get("content_type") or "").strip().lower()
        if content_type not in ALLOWED_EVIDENCE_TYPES:
            raise ValueError(
                f"Unsupported evidence type: {content_type or 'unknown'}. "
                "Attach a PDF, image, text, CSV or Office document."
            )
        content = _decode(item.get("data") or "")
        if not content:
            raise ValueError(f"{filename} is empty")
        if len(content) > MAX_EVIDENCE_BYTES:
            raise ValueError(
                f"{filename} is larger than {MAX_EVIDENCE_BYTES // (1024 * 1024)} MB"
            )
        evidence.append(Evidence(
            filename=filename,
            content_type=content_type,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
        ))
    return evidence
