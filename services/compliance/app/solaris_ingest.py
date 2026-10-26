"""Solaris assessment ingestion used by the compliance API.

Solaris is collected read-only. Assessments are either dropped into the
inventory service's watched inbox (preferred, batch) or posted here by the
approved collector (interactive / scripted). Both paths record an
immutable ``compliance.solaris_uploads`` row and the resulting assessment.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from sqlalchemy import text

from patchtrack_common.db import connection

log = logging.getLogger(__name__)

SEVERITY_MAP = {
    "critical": "critical", "security": "critical",
    "important": "important", "recommended": "moderate",
    "moderate": "moderate", "low": "low", "optional": "low",
}


def _resolve_asset(conn, record: dict[str, Any]) -> str | None:
    hostname = record.get("hostname")
    fqdn = record.get("fqdn")
    ip = record.get("ip_address")
    for column, value in (("fqdn", fqdn), ("hostname", hostname)):
        if not value:
            continue
        rows = conn.execute(
            text(
                f"SELECT id FROM inventory.assets WHERE lower({column}) = lower(:v)"
            ),
            {"v": value},
        ).all()
        if len(rows) == 1:
            return str(rows[0][0])
        if len(rows) > 1:
            return None
    if ip:
        rows = conn.execute(
            text("SELECT id FROM inventory.assets WHERE ip_address = CAST(:v AS inet)"),
            {"v": ip},
        ).all()
        if len(rows) == 1:
            return str(rows[0][0])
    return None


def ingest_solaris_records(
    records: list[Any], *, uploaded_by: str
) -> dict[str, Any]:
    accepted = rejected = 0
    problems: list[str] = []
    payload_digest = hashlib.sha256(
        json.dumps([r.model_dump(mode="json") for r in records],
                   sort_keys=True).encode()
    ).hexdigest()

    with connection() as conn:
        run = conn.execute(
            text(
                "INSERT INTO inventory.sync_runs "
                "(integration_key, trigger, requested_by) "
                "VALUES ('solaris', 'manual', :by) RETURNING id"
            ),
            {"by": uploaded_by},
        ).first()
        run_id = str(run[0]) if run else None
        for record in records:
            data = record.model_dump(mode="json")
            asset_id = _resolve_asset(conn, data)
            if asset_id is None:
                rejected += 1
                problems.append(
                    f"{data.get('hostname')}: no unique inventory match"
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO inventory.reconciliation_conflicts
                            (source_system, source_ref, match_key, reason, payload)
                        VALUES ('solaris', :ref, :key, 'assessment_unmatched',
                                CAST(:p AS jsonb))
                        """
                    ),
                    {
                        "ref": data.get("hostname") or "",
                        "key": data.get("fqdn") or data.get("hostname") or "",
                        "p": json.dumps(data, default=str),
                    },
                )
                continue
            counts = {"critical": 0, "important": 0, "moderate": 0, "low": 0,
                      "unknown": 0}
            for patch in data.get("patches", []):
                severity = SEVERITY_MAP.get(
                    str(patch.get("severity") or "moderate").lower(), "unknown"
                )
                counts[severity] += 1
            assessment_id = str(
                conn.execute(
                    text(
                        """
                        INSERT INTO compliance.assessments (
                            asset_id, source_system, assessed_at, sync_run_id,
                            status, total_patches, critical_count, important_count,
                            moderate_count, low_count, source_reference, details
                        ) VALUES (
                            :a, 'solaris', :assessed, :run, 'complete', :total,
                            :crit, :imp, :mod, :low, :ref, CAST(:details AS jsonb)
                        ) RETURNING id
                        """
                    ),
                    {
                        "a": asset_id, "assessed": data["assessed_at"],
                        "run": run_id, "total": len(data.get("patches", [])),
                        "crit": counts["critical"], "imp": counts["important"],
                        "mod": counts["moderate"], "low": counts["low"],
                        "ref": f"solaris:{data.get('hostname')}",
                        "details": json.dumps({"uploaded_by": uploaded_by}),
                    },
                ).first()[0]
            )
            for patch in data.get("patches", []):
                severity = SEVERITY_MAP.get(
                    str(patch.get("severity") or "moderate").lower(), "unknown"
                )
                conn.execute(
                    text(
                        """
                        INSERT INTO compliance.findings (
                            asset_id, assessment_id, source_system, advisory_id,
                            title, severity, category, released_at, source_reference
                        ) VALUES (:a, :asmt, 'solaris', :adv, :title, :sev, :cat,
                                  :rel, :ref)
                        ON CONFLICT (assessment_id, advisory_id) DO NOTHING
                        """
                    ),
                    {
                        "a": asset_id, "asmt": assessment_id,
                        "adv": str(patch.get("patch_id")),
                        "title": str(patch.get("title") or ""),
                        "sev": severity,
                        "cat": str(patch.get("category") or "patch"),
                        "rel": patch.get("released_at"),
                        "ref": str(patch.get("url") or ""),
                    },
                )
            conn.execute(
                text(
                    """
                    INSERT INTO compliance.asset_latest_assessment
                        (asset_id, assessment_id, updated_at)
                    VALUES (:a, :asmt, now())
                    ON CONFLICT (asset_id) DO UPDATE
                      SET assessment_id = EXCLUDED.assessment_id,
                          updated_at = now()
                    """
                ),
                {"a": asset_id, "asmt": assessment_id},
            )
            accepted += 1

        if run_id:
            conn.execute(
                text(
                    "UPDATE inventory.sync_runs SET status = :s, finished_at = now(), "
                    "records_seen = :seen, records_updated = :ok, "
                    "records_rejected = :bad, assets_unmatched = :bad "
                    "WHERE id = :id"
                ),
                {
                    "s": "success" if rejected == 0 else "partial",
                    "seen": len(records), "ok": accepted, "bad": rejected,
                    "id": run_id,
                },
            )
        conn.execute(
            text(
                """
                INSERT INTO compliance.solaris_uploads
                    (filename, sha256, uploaded_by, status, records, message)
                VALUES (:f, :h, :by, :s, :n, :m)
                """
            ),
            {
                "f": f"api-upload-{payload_digest[:12]}.json", "h": payload_digest,
                "by": uploaded_by,
                "s": "accepted" if accepted else "rejected",
                "n": accepted, "m": "; ".join(problems)[:1000],
            },
        )
    return {
        "accepted": accepted, "rejected": rejected,
        "problems": problems, "run_id": run_id,
    }
