# Requirements traceability matrix

Maps every business requirement to the artefact that implements it and the
check that verifies it. Status meanings:

- **Implemented + tested** — code exists and an automated test exercises it.
- **Implemented, environment-dependent** — code exists; end-to-end behaviour
  depends on a source system or runtime this repository cannot reach.
- **Implemented, procedure-verified** — code exists and was verified by a
  documented manual procedure rather than an automated test.

## Business requirements

| Req | Implementation | Verification | Status |
|-----|----------------|--------------|--------|
| BR-1 Authoritative inventory | `services/inventory/app/sync.py` (`upsert_asset`, `run_assetexplorer_sync`), `connectors/assetexplorer.py` | `test_reconciliation.py` (create/rename/alias), `test_engine.py` population rules | Implemented, environment-dependent (live AssetExplorer field names unverified) |
| BR-2 Authoritative patch assessment | `connectors/satellite.py`, `sccm.py`, `solaris.py`; `sync.ingest_assessments` | `test_reconciliation.py` matching tests | Implemented, environment-dependent |
| BR-3 Five compliance states | `compliance/app/engine.py` | `test_engine.py` — one test per state plus ordering | Implemented + tested |
| BR-4 Documented numerator/denominator | `engine.Population`, `methodology` in `evaluator.create_snapshot`, PDF methodology section | `test_engine.py` denominator tests; `test_report_pdf.py` | Implemented + tested |
| BR-5 Freshness | `engine.evaluate_asset` (`exact_age_hours`) | `test_engine.py` boundary tests | Implemented + tested |
| BR-6 Scope and exclusion | `engine.evaluate_asset` rules 1; `inventory /assets/{id}/exclude` | `test_engine.py` scope tests; audit assertions | Implemented + tested |
| BR-7 Immutable history | `evaluator.create_snapshot`; `snapshots_daily_unique_idx`; no update path | `test_reconciliation.py` audit immutability; schema review | Implemented, procedure-verified |
| BR-8 Reporting + digest | `reporting/app/pdf.py`, `service.generate_report` | `test_report_pdf.py` (valid, digest, truncation, empty) | Implemented + tested (layout not asserted) |
| BR-9 Delivery isolation | `reporting/app/mailer.py`, `service.deliver_report`, `reporting.delivery_attempts` | Code review; SMTP path not exercised | Implemented, environment-dependent |
| BR-10 Dashboard | `gateway/static/*`; `deploy/grafana/*` | `tests/ui/smoke.py`; dashboard verification procedure | Implemented, procedure-verified |
| BR-11 Roles | `patchtrack_common/security.py`; `require_permission` on every route | `test_security.py` | Implemented + tested |
| BR-12 Audit | `patchtrack_common/audit.py`; `shared.audit_events` + trigger | `test_reconciliation.py` append-only test | Implemented + tested |
| BR-13 Integrations explicit | migration 006 (disabled rows); `inventory /integrations` | Schema review; `test_secrets.py` | Implemented + tested |
| BR-14 Portability | `deploy/container/*` (UBI9), `docker-compose.yml` | Not executed here (no Docker) | Implemented, environment-dependent |

## Constraints

| Constraint | Control | Evidence |
|------------|---------|----------|
| C-1 No universal default credentials | No seeded users; bootstrap refuses when a user exists; `.env.example` contains only `CHANGE_ME` placeholders | migration 002 has no `INSERT`; `identity /bootstrap/status`; `gateway /api/session/bootstrap` |
| C-2 Do not record secrets | `audit.scrub` denylist; no request bodies stored | `test_security.py`; `audit.py` |
| C-3 No production credentials in source control | `.env` gitignored; `secret_ref` accepts references only | `secrets.validate_reference`; `test_secrets.py` |
| C-4 Never disable TLS verification | `verify_tls` defaults true; plain HTTP requires an explicit, audited opt-out; `sslmode` requires TLS | `inventory /integrations/{key}`; `test_secrets.py` |
| C-5 SELinux not required to be disabled | `:Z` mounts; no `setenforce` anywhere; troubleshooting documents the correct fix | `docker-compose.yml`; `troubleshooting.md` |
| C-6 No assumed SSH collection | No SSH client import anywhere; Solaris is a file drop | `connectors/solaris.py`; code search |
| C-7 No silent wrong association | `sync.match_asset` returns `(None, "ambiguous")`; conflicts table | `test_reconciliation.py` ambiguity test |
| C-8 No silent report replacement | `generate_report` returns early when completed; partial unique index; retry renders the same snapshot | `test_report_pdf.py` determinism; `reporting/reports` constraints |
| C-9 No static or fabricated data | The SPA calls live APIs; demo data is opt-in, synthetic and marked `raw.demo_data`; empty states render as `n/a` | `test_report_pdf.py` empty population; `scripts/seed-demo.py` docstring |
| C-10 Failed email does not invalidate a report | `deliver_report` writes only `delivery_attempts`; report status untouched | `mailer.send_report_email` returns `(status, detail)`; code review |
| C-11 No fabricated attributes | Connectors store empty/`UNKNOWN` for missing fields; no inference | `connectors/*.normalise`; `test_engine.py` |
| C-12 Unknown/stale never compliant | `engine.evaluate_asset` rules 2–3 keep them out of `compliant` and in the denominator | `test_engine.py` |

## Functional requirements

| Range | Document | Implemented | Notes |
|-------|----------|-------------|-------|
| FR-A1..A10 authentication | [functional-requirements.md](functional-requirements.md#authentication-and-session-fr-a) | 10/10 | Login lockout, Argon2id, cookie session, bootstrap |
| FR-B1..B5 users and roles | same | 5/5 | Last-administrator guard is in `identity.update_user` |
| FR-C1..C12 inventory | same | 12/12 | C1 field map is configuration-driven; C11 override survives a sync (integration test); C12 rename propagation through aliases, sync survival and the delete refusal are integration-tested |
| FR-D1..D8 reconciliation | same | 8/8 | D7 resolution route + audit |
| FR-E1..E7 integrations | same | 7/7 | E3 rejects pasted credentials |
| FR-F1..F14 evaluation | same | 14/14 | F13 pinned by `test_period_parity.py` |
| FR-G1..G5 snapshots | same | 5/5 | G1 idempotency via partial unique index |
| FR-H1..H14 reporting | same | 14/14 | H10 pinned by `test_scheduler.py` |
| FR-I1..I11 dashboard/API | same | 11/11 | I5 verified by stopping Grafana; I8/I9/I10 exercised in the browser smoke test; I11 pinned by `test_dashboard_breakdown.py` |
| FR-J1..J4 audit | same | 4/4 | J1 enforced by a trigger |
| FR-K1..K5 operations | same | 4/5 | K5 offline bundle documented; the bundling script itself is operator-authored (§ below) |

## Section 22 artefacts

| Artefact | Location |
|----------|----------|
| Requirements traceability matrix | this document |
| User stories and acceptance criteria | [user-stories.md](user-stories.md) |
| Implementation roadmap | [roadmap.md](roadmap.md) |
| Testing strategy | [testing-strategy.md](testing-strategy.md) |

## Known gaps and their owners

| Gap | Impact | Owner to close |
|-----|--------|----------------|
| AssetExplorer/Satellite/SCCM field names unverified against live systems | Collections may need `extra.field_map` or an endpoint adjustment before they work | Deployment engineer, with the source-system administrator |
| No Docker execution on the build host | Compose and SELinux behaviour unproven here | Deployment engineer on the target RHEL/Rocky 9.8 host |
| systemd units never started | `deploy/systemd/*.service` pass `systemd-analyze verify`, but no `patchtrack` account or postgresql-13 service exists on the build host | Deployment engineer, on the target |
| SMTP path untested | Delivery failure handling is code-reviewed, not exercised | Operations, with a test relay |
| OIDC sign-in absent | Enterprise identity integration requires code | Product owner, next release |
| No MFA | Password-only sign-in | Product owner, next release |
| Dashboard exporter absent | Monitoring is query-based, not Prometheus | Operations |
| `patchtrack_grafana` password is per-deployment | The role was created and its grants exercised on the build host, but the password is generated at install time and never shipped; the target runs `010_grafana_readonly.sql` itself | Deployment engineer, on the target |
| No performance measurement at scale | Sizing guidance is an estimate | Operations, during first production month |
