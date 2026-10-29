# Troubleshooting

Start with the symptom, then the evidence to collect, then the cause. Do not
change `.env` secrets, disable TLS or disable SELinux to "see if it helps" —
each of those turns a small problem into a security incident.

Where the evidence is written is in [logging.md](logging.md): service and
PostgreSQL logs, the backup worker, and the `install.sh`/`upgrade.sh`/
`uninstall.sh` run logs under `/var/log/patchtrack/`. For a planned shutdown or
restart, [start-stop.md](start-stop.md) gives the graceful order.

## Services do not start

```
Container patchtrack-identity-1  Error
```

```bash
cd /opt/patchtrack
docker compose logs identity | tail -40
```

| Message | Cause | Fix |
|---------|-------|-----|
| `PATCHTRACK_DATABASE_URL` validation error | Variable missing or empty | Set it in `/etc/patchtrack/.env` |
| `jwt_secret: Field required` / `too short` | Secret missing or < 32 chars | `openssl rand -hex 32` |
| `password authentication failed` | DB password mismatch with `POSTGRES_PASSWORD` | Make the two identical, restart postgres **then** services |
| `connection refused` to `postgres:5432` | Started before postgres was healthy | `docker compose up -d postgres`, wait for healthy, then `docker compose up -d` |
| `relation "shared.schema_migrations" does not exist` | Migrations not run | `docker compose run --rm migrate` |
| `PATCHTRACK_SERVICE is required` | Entrypoint used outside compose | Set it, or use compose |

The `migrate` container exiting non-zero blocks every dependent service by
design. Check its log first.

## Migrations fail

```
Migration 004_compliance was modified after being applied
```

The file no longer matches the checksum in `shared.schema_migrations`. Applied
migrations are immutable. Restore the file from git — do not update the
checksum row.

```
permission denied for schema ...
```

The application role does not own the schemas. Connect as the owner or grant
the needed privileges; do not run migrations as `postgres` and then start the
services as a different role.

```
deadlock detected` / `canceling statement due to lock timeout`
```

Another connection holds a lock. Stop the services, identify the holder with
`SELECT pid, query FROM pg_stat_activity WHERE state <> 'idle';` and retry.

## Integration sync fails

**Operational health** shows the state; **sync runs** shows the error.

### `AssetExplorer authentication failed (HTTP 401)`

The token has expired or the reference resolves to nothing.

```bash
docker compose exec -T inventory python -c \
  "from patchtrack_common.secrets import resolve; print(bool(resolve('env:PATCHTRACK_ASSETEXPLORER_TOKEN')))"
```

`False` means the environment variable is not visible **inside the container**.
Check `.env` and restart the service (`docker compose up -d --force-recreate inventory`).

### `request failed: [SSL: CERTIFICATE_VERIFY_FAILED]`

The issuing CA is not trusted inside the container. Do the right thing:

```bash
sudo cp enterprise-root-ca.crt /etc/pki/ca-trust/source/anchors/
sudo update-ca-trust extract
```

and mount the host trust store into the service, or add the CA to the image at
build time. **Do not** set `verify_tls=false` to work around it; that is
audited and is the wrong answer for a production system.

### `assetexplorer` sync succeeds but creates zero assets

The response shape does not match the connector's field map. Fetch one record
and compare:

```bash
docker compose exec -T inventory python - <<'PY'
import json, os, urllib.request
req = urllib.request.Request("https://assetexplorer.example.com/api/v3/assets/servers?page=1&size=1",
    headers={"Authorization": f"Bearer {os.environ['PATCHTRACK_ASSETEXPLORER_TOKEN']}"})
print(json.dumps(json.load(urllib.request.urlopen(req))["data"][0], indent=2))
PY
```

Set `extra.field_map` on the integration to match. See
[integration-assetexplorer.md](integration-assetexplorer.md#field-mapping).

### Sync run is `partial` with `assets_unmatched`

The patch source reports hosts that AssetExplorer does not have. That is a data
problem, not a PatchTrack problem:

```sql
SELECT source_system, reason, count(*)
FROM inventory.reconciliation_conflicts WHERE resolved_at IS NULL
GROUP BY 1, 2 ORDER BY 3 DESC;
```

Add the missing hosts to AssetExplorer. Do not invent them in PatchTrack.

### Everything reports Unknown after a successful sync

Check `assessed_at`, not `collected_at`:

```sql
SELECT a.hostname, s.source_system, s.assessed_at, s.status
FROM compliance.assessments s JOIN inventory.assets a ON a.id = s.asset_id
ORDER BY s.assessed_at DESC LIMIT 10;
```

If `assessed_at` is old (or in the future), the source is returning a bad
timestamp. A future timestamp will look fresh forever — investigate it as a data
defect.

## Compliance percentage looks wrong

| Observation | Likely cause |
|-------------|--------------|
| Lower than another tool | Unknown/stale are in the denominator here; and the populations may differ |
| `n/a` | Population is empty — no assets synced, or all filtered out |
| Dropped suddenly | A source stopped collecting; check freshness and integration health |
| Doesn't match the PDF | You are comparing different periods, or a policy change happened between them |
| Doesn't match Grafana | Grafana is filtered differently (check the template variables) |

Verify the arithmetic directly:

```sql
SELECT total_servers, compliant_servers, non_compliant_servers,
       unknown_servers, stale_servers, excluded_servers, compliance_pct
FROM compliance.snapshots WHERE scope = 'daily'
ORDER BY period_start DESC LIMIT 1;
```

`compliance_pct` should equal `compliant / (total − excluded) × 100`.

"Unknown" after a policy change is normal for a moment; activating a policy
re-evaluates immediately.

## Reports

### `409 No compliance snapshot exists for that period yet`

Capture the snapshot first (Reports → Capture snapshot), then generate. A daily
snapshot alone does not constitute a monthly period.

### Report stuck in `generating`

A generation crashed between the status update and the final write. Look at the
reporting log; then retry the report, which re-renders the same snapshot.

```bash
docker compose logs reporting | grep -i "report generation" | tail
```

### `failed` with "Generated PDF is implausibly small" or "PDF is truncated"

ReportLab raised or returned partial output, usually because a table had a
column wider than the frame or a font was missing. The exception is recorded in
`error_summary`. Attach it when reporting the defect.

### Email not delivered

```sql
SELECT r.id, r.status, d.status AS delivery, d.detail
FROM reporting.delivery_attempts d JOIN reporting.reports r ON r.id = d.report_id
ORDER BY d.attempted_at DESC LIMIT 5;
```

`skipped / SMTP is not configured` — save a relay at **Settings → Email
delivery**, or set `PATCHTRACK_SMTP_HOST` and restart if the installation uses
the environment fallback.
`failed / The SMTP password is not available` — the saved password cannot be
decrypted, usually after a secrets-key or JWT-secret rotation. Re-enter it in
Settings.
`failed` — the detail names the SMTP error. Check relay allow-listing for the
sender address. **Send test email** on the same screen isolates relay
connectivity from report generation.

The report is `completed` regardless. This is intentional: a failed email never
invalidates a report.

### Report downloads 404 after a restore

The metadata row exists but the file is not on the volume. Restore the report
volume from the same backup point (see
[backup-restore.md](backup-restore.md#42-restore-the-report-volume)).

## Browser problems

### Immediately bounced back to the login page

- The session cookie is `Secure` on non-loopback hosts. Accessing the site over
  plain HTTP on a hostname (not `localhost`) will drop the cookie. Use HTTPS.
- The cookie is `SameSite=Strict`; opening PatchTrack in an iframe or from a
  cross-site link requires a fresh navigation.
- Clock skew between the browser and the server makes the JWT appear expired.

### The browser warns the certificate is not trusted

Expected until the organisation's certificate is installed: the first start
generated a self-signed one. Sign in, open **Settings → HTTPS certificate** and
upload the issued certificate with its chain and the matching unencrypted key.
The warning disappears on the next connection, without a restart. Do not work
around it by clicking through in production, and do not turn off redirects or
the `Secure` cookie policy.

If the certificate shown is not the one you uploaded, check whether the
gateway was started with an external process (for example the plain uvicorn
CLI rather than `python -m patchtrack_common.serve`) or with TLS disabled: the
files are stored, but only the launcher reloads the live listener. Restart the
gateway, or use the certificate at the proxy when one terminates TLS.

### The gateway does not start: `incomplete TLS material`

The certificate and key must be replaced together. If one file is missing, the
gateway refuses to start rather than overwrite the other half. Restore the pair
from backup, install a pair through Settings, or — only if the existing
certificate is disposable — remove both files so a fresh self-signed pair is
generated on the next start. Check the key's mode is 0600 and that the service
account can read the directory.

### `PUT /api/tls` returns 409

The gateway is not terminating TLS (`PATCHTRACK_TLS_ENABLED=false`); an
external proxy or load balancer is responsible for certificates. Install it
there. Uploading through PatchTrack would store files that nothing serves.

### `422` when uploading a certificate

The pair was rejected before anything was written; the existing certificate is
still in use. The message names the reason: malformed PEM, a key that does not
match the certificate, an expired or not-yet-valid certificate, or a
passphrase-protected key. Export the key unencrypted (PKCS#9/`BEGIN PRIVATE
KEY`) and retry with the full chain.

### `403 CSRF validation failed`

The SPA reads the `patchtrack_csrf` cookie and echoes it in
`x-patchtrack-csrf`. A proxy that strips cookies, or a browser extension that
blocks cookies for the site, causes this. Confirm both cookies exist in the
browser's developer tools.

### `403 Cross-origin request rejected`

The proxy is forwarding an `Origin` header that does not match the `Host`. Set
`X-Forwarded-Host` at the proxy, or make the proxy rewrite `Origin` to the
external host. Do not add a wildcard.

### Charts render as empty boxes

The console will show a script error. The CSP forbids inline scripts; a
modified page that injects one will be blocked. Revert the customisation.

## Grafana problems

### `password authentication failed for user "patchtrack_grafana"`

The role was never created, or the password in `.env` differs from the one
used at creation. Re-run the role script with the current password —
it rotates the password in place:

```bash
docker compose exec -T postgres psql -v ON_ERROR_STOP=1 \
  -v grafana_password="$PATCHTRACK_GF_DB_PASSWORD" -U patchtrack -d patchtrack \
  < deploy/postgres/roles/010_grafana_readonly.sql
```

### Dashboard shows "No data" everywhere

- Template variables: an "All" selection depends on the `__all__` sentinel. If
  someone edited the dashboard, restore it from
  `deploy/grafana/dashboards/patchtrack-overview.json` — it is provisioned and
  UI edits are discarded.
- The datasource may point at the wrong database. Check
  `deploy/grafana/provisioning/datasources/patchtrack.yaml` and the
  `PATCHTRACK_GF_DB_*` variables.

### Grafana is down — is the application affected?

No. Stop Grafana at any time; the application, API, reports and schedules are
independent.

## Database problems

### Too many connections

```sql
SELECT usename, count(*) FROM pg_stat_activity GROUP BY 1 ORDER BY 2 DESC;
```

Five services at pool 5 + overflow 5 plus Grafana can approach PostgreSQL's
default 100. Lower `PATCHTRACK_DB_POOL_MAX_OVERFLOW` or raise
`max_connections`.

### Audit events cannot be deleted

Working as designed. The trigger `audit_events_no_update` raises on `UPDATE` and
`DELETE`. Use the audit API for retrieval.

### Report generation is slow

`reporting.reports.started_at` → `completed_at` in the log. Slow generation is
almost always the trend query over a long snapshot history; it is bounded by
the indexed `compliance.snapshots (scope, period_start)`.

## TLS verification failures

Never the answer: `verify_tls=false`, `sslmode=disable`, or
`curl -k`.

The answer is one of:

1. Install the issuing CA into the trust store the service uses
   (`/etc/pki/ca-trust/source/anchors/` + `update-ca-trust`), and mount it into
   the container.
2. Use the correctly issued certificate for the hostname being contacted — a
   certificate for the load balancer's name is not valid for the node behind it.
3. Confirm SNI and the `Host` header match the certificate.

Disabling verification is recorded in the audit trail as
`integration.tls_verification_disabled` and will be raised in the next audit.

## SELinux denials

Check:

```bash
sudo ausearch -m avc -ts recent | grep -i patchtrack
```

Common cases:

| Denial | Cause | Fix |
|--------|-------|-----|
| Container cannot write the report volume | Volume not relabelled | Ensure `:Z` on the bind mount (already in `docker-compose.yml`) |
| Container cannot read a credential file | File not relabelled or mode too open | `sudo chcon -t container_file_t /etc/patchtrack/secrets/satellite.password`, mode 0600 |
| Native install cannot write `/var/lib/patchtrack` | Wrong type or ownership | `sudo chown -R patchtrack:patchtrack`, keep `var_lib_t` |

Do **not** run `setenforce 0`. If you cannot resolve a denial, capture the AVC,
the container's mount list and `ls -Z` of the path, and escalate.

## Slow or heavy collection

- Satellite and SCCM syncs make one request per host, serially. A large estate
  takes minutes; that is normal.
- If a run exceeds `read_timeout_s`, raise it rather than lowering
  `page_size` first; then check the source's own load.
- Run collections outside business hours on large estates.

## Getting a good bug report

```bash
cd /opt/patchtrack
docker compose ps
docker compose logs --since 1h --no-color > /tmp/patchtrack-logs.txt
docker compose exec -T postgres psql -U patchtrack -d patchtrack -c \
  "SELECT version, applied_at FROM shared.schema_migrations ORDER BY version;"
docker compose exec -T postgres psql -U patchtrack -d patchtrack -c \
  "SELECT integration_key, status, started_at, error_summary
     FROM inventory.sync_runs ORDER BY started_at DESC LIMIT 10;"
```

Include the screen, the filters, the period and the hostname/report id. Exclude
`.env`, credential files and database dumps.
