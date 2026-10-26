# Operations runbook

## 1. Daily checks (5 minutes)

```bash
cd /opt/patchtrack
docker compose ps
curl -fsS http://127.0.0.1:8005/healthz
```

Then in the UI:

| View | What to look for |
|------|------------------|
| Dashboard | Percentage, unknown/stale counts, "last synced" age |
| Operational health | Every enabled integration `healthy` or `degraded` with a reason |
| Reconciliation | Open conflicts count, and whether they are new |
| Reports (if scheduled) | Yesterday's/this month's report `completed`, delivery `sent` |

Red flags:

- unknown + stale rising for a source that should be collecting;
- an integration `authentication_failure` (credential expired);
- `stale` integration health (collection silently stopped);
- an open reconciliation conflict on a hostname that matters.

## 2. Health endpoints

| Endpoint | Meaning |
|----------|---------|
| `/healthz` | Process is up |
| `/readyz` | Process can reach PostgreSQL |

`readyz` is the one to use in monitoring; `healthz` is for the container
`HEALTHCHECK`.

```bash
for port in 8001 8002 8003 8004 8005; do
  printf '%s ' "$port"; curl -fsS "http://127.0.0.1:$port/readyz" || echo DOWN; echo
done
```

## 3. Scheduled jobs

Jobs are HTTP calls to the owning service. The gateway's `/api` routes require a
browser session cookie, which is the wrong credential for a timer, so jobs call
the service directly and present the shared service token instead. In every
case they act as the `scheduler` principal — never as a named human user — so
the audit trail answers "who ran what" without a shared account.

Two scripts, one job each versus everything due:

| Script | What it runs | How the request is made |
|--------|--------------|-------------------------|
| `scripts/patchtrack-job.sh` | One named job (`sync …`, `daily`, `tick`, `purge`) — the cron shape | Detects the deployment per call: `docker compose exec` (or `podman-compose exec` / `podman exec`, the usual runtime on RHEL and Rocky) into the service's own container where one is running, otherwise a direct request to `127.0.0.1:$PORT`. `PATCHTRACK_MODE=container\|native` overrides the detection; `PATCHTRACK_CONTAINER_CLI=docker\|podman` pins the runtime when both are installed |
| `scripts/patchtrack-tick.sh` | Every job not yet due — the timer shape | Direct request to `127.0.0.1:$PORT` with the token from `/etc/patchtrack/.env`; one hourly run covers every cadence |

Both exit non-zero when any job reports a failure, so cron mail and systemd
unit failure states work without extra plumbing.

### Containers (cron or timer)

```bash
sudo -u patchtrack /opt/patchtrack/scripts/patchtrack-job.sh sync assetexplorer
sudo -u patchtrack /opt/patchtrack/scripts/patchtrack-job.sh sync satellite
sudo -u patchtrack /opt/patchtrack/scripts/patchtrack-job.sh daily
sudo -u patchtrack /opt/patchtrack/scripts/patchtrack-job.sh tick
```

```cron
# /etc/cron.d/patchtrack  — mode 0644, root-owned.
SHELL=/bin/bash
PATCHTRACK_JOB=/opt/patchtrack/scripts/patchtrack-job.sh

15 */6 * * *  patchtrack  $PATCHTRACK_JOB sync assetexplorer
30 */6 * * *  patchtrack  $PATCHTRACK_JOB sync satellite
0  3  * * *   patchtrack  $PATCHTRACK_JOB sync sccm
0  4  * * 1   patchtrack  $PATCHTRACK_JOB sync solaris
30 0  * * *   patchtrack  $PATCHTRACK_JOB daily
5  *  * * *   patchtrack  $PATCHTRACK_JOB tick
```

The script reads `PATCHTRACK_SERVICE_TOKEN` from **inside the service
container** (loaded from `.env` by compose), so the calling account never needs
to hold the token. Rotating the token remains a coordinated change across all
five services.

### Native (systemd timer — the preferred form where containers are prohibited)

`scripts/patchtrack-tick.sh` decides what is actually due on each run; it is
driven by two unit files that ship in `deploy/systemd/` alongside the service
units. Nothing runs until the timer is enabled, so installing them is not the
same as scheduling anything.

| File | What it does |
|------|--------------|
| `patchtrack-tick.service` | `Type=oneshot`; runs the script as `patchtrack` with `/etc/patchtrack/.env` (token) and the optional `/etc/patchtrack/tick.env` (cadences); writable only in `/var/lib/patchtrack` |
| `patchtrack-tick.timer` | `OnCalendar=hourly`, `Persistent=true` so a run missed while the host was down happens once after boot |

```bash
sudo install -m 0755 scripts/patchtrack-tick.sh /opt/patchtrack/scripts/
sudo install -d -m 0750 -o patchtrack -g patchtrack /var/lib/patchtrack/state
sudo systemctl daemon-reload
sudo systemctl enable --now patchtrack-tick.timer
```

Cadence is configuration, in `/etc/patchtrack/tick.env`:

```ini
PATCHTRACK_SYNC_ASSETEXPLORER_INTERVAL=21600   # seconds
PATCHTRACK_SYNC_SATELLITE_INTERVAL=21600
PATCHTRACK_SYNC_SCCM_INTERVAL=86400
PATCHTRACK_SYNC_SOLARIS_INTERVAL=604800
```

The daily snapshot runs at most once per 24 hours and the report tick runs on
every timer firing. A stamp is written only on success, so a failed collection
retries on the next tick rather than waiting a full interval; a failure in one
step does not prevent the others from running (a dead SCCM endpoint must not
stop the daily snapshot). Output distinguishes `ok`, `skip … (not due)` and
`FAIL`, and the unit fails if anything reported `FAIL`.

Check with `systemctl list-timers patchtrack-*` and
`journalctl -u patchtrack-tick --since yesterday`.

## 4. Common operations

### Run a collection now

UI: **Operational health → Run now**. Or:

```bash
curl -fsS -X POST -H "Authorization: Bearer $(cat /etc/patchtrack/secrets/service.token)" \
  -H 'Content-Type: application/json' -d '{"integration":"satellite"}' \
  http://127.0.0.1:8005/api/inventory/sync
```

### See why a collection failed

```bash
curl -fsS "http://127.0.0.1:8005/api/inventory/sync-runs?limit=5" \
  -H "Authorization: Bearer $(cat /etc/patchtrack/secrets/service.token)" | jq
```

The `error_summary` is deliberately short and free of secrets.

### Re-evaluate compliance after a policy change

Activating a policy version re-evaluates automatically. To force it:

```bash
curl -fsS -X POST -H "Authorization: Bearer …" http://127.0.0.1:8005/api/compliance/evaluate
```

### Inspect local placement overrides

An override corrects the environment or business service for one asset without
editing AssetExplorer. Saving one already re-evaluates compliance; nothing else
is required, and the sync is unaffected. To see every active override:

```bash
psql -d patchtrack -c "
  SELECT a.hostname, o.environment, a.environment AS source_environment,
         o.business_group, a.business_group AS source_business_group,
         o.reason, o.set_by, o.set_at
  FROM inventory.asset_overrides o
  JOIN inventory.assets a ON a.id = o.asset_id
  ORDER BY o.set_at DESC"
```

To remove one without the UI, delete the row (`DELETE FROM
inventory.asset_overrides WHERE asset_id = …`); the source value applies at
once. Prefer the UI so the action is attributed in the audit trail.

### Inspect the placement picker lists

Environment and business-service values offered in the editor are administered
in **Settings → Inventory placement values**. To inspect them, including how
many servers currently hold each value (source or override):

```bash
psql -d patchtrack -c "
  SELECT kind, value,
         CASE kind
           WHEN 'environment' THEN (SELECT count(*) FROM inventory.asset_effective a
                                    WHERE a.environment = o.value)
           ELSE (SELECT count(*) FROM inventory.asset_effective a
                 WHERE a.business_group = o.value)
         END AS in_use
  FROM inventory.placement_options o
  ORDER BY kind, lower(value)"
```

Renaming a row propagates to every server that holds the old spelling through
`inventory.placement_aliases`, resolved at read time; the source value in
`inventory.assets` is not rewritten, so the next sync cannot undo it. A row
whose `in_use` is above zero cannot be deleted — rename it instead. Remove a
stale alias only after the source system reports the new name:

```sql
DELETE FROM inventory.placement_aliases
WHERE kind = 'environment' AND lower(btrim(alias)) = lower('old name');
```

Changing the placement of one server by hand remains a separate action in
**Server inventory**.

### Capture a snapshot for a past period

UI: **Reports → Capture snapshot**, or:

```bash
curl -fsS -X POST -H "Authorization: Bearer …" -H 'Content-Type: application/json' \
  -d '{"scope":"monthly","reporting_month":"2026-09"}' \
  http://127.0.0.1:8005/api/compliance/snapshots
```

A monthly snapshot captures the **current** state, labelled with the period.
It is not a reconstruction of September. If September's daily snapshots were
not captured, there is no September history and the report will say so.

### Generate a report

**Reports → Generate**, selecting the month/quarter. Requires an existing
snapshot for that period, otherwise the API returns 409 with an explanation.

### Investigate a reconciliation conflict

1. **Reconciliation** view: the conflict shows source system, source reference,
   match key and a reason.
2. `ambiguous_match` — two assets share the hostname/FQDN/IP. Fix the
   AssetExplorer record (the duplicate), or resolve the conflict by choosing
   the correct asset.
3. `identifier_changed` — a patch source identifier now points at a different
   host than before. Confirm with the patch team before resolving.
4. `assessment_unmatched` — the patch source reports a host that is not in
   AssetExplorer. Add it to AssetExplorer; do not create it by hand.

Resolution requires a note and is audited.

### Exclude a decommissioned server

Only if AssetExplorer has not already retired it. **Inventory → server →
Exclude**, with a reason. The asset leaves the denominator, retains its
history, and the exclusion is audited. Prefer fixing AssetExplorer.

## 5. Running natively under systemd

If containers are prohibited, run each service as a systemd unit. The units
are in `deploy/systemd/`; the templates in this section are copied from them,
so the repository is the source of truth if the two ever drift.

`patchtrack@.service` takes the instance name (`%i`) as the service directory
under `/opt/patchtrack/services`. Migration is a separate oneshot unit that
every instance requires, so two services never race on DDL and a failed
migration leaves the services down rather than half-migrated:

```bash
sudo install -m 0644 deploy/systemd/patchtrack@.service \
  deploy/systemd/patchtrack-migrate.service /etc/systemd/system/
sudo install -d -m 0755 /etc/patchtrack/ports
for spec in identity:8001 inventory:8002 compliance:8003 reporting:8004 gateway:8005; do
  printf 'PORT=%s\n' "${spec#*:}" | sudo tee "/etc/patchtrack/ports/${spec%%:*}" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now patchtrack-migrate.service
sudo systemctl enable --now patchtrack@identity patchtrack@inventory \
  patchtrack@compliance patchtrack@reporting patchtrack@gateway
```

The unit has **no** port default: a missing `/etc/patchtrack/ports/<instance>`
is a start-up failure, not a service listening on an unintended port. The
environment comes from `/etc/patchtrack/.env` (mode 0600, root:patchtrack),
including `PATCHTRACK_FORWARDED_IPS` for `--forwarded-allow-ips`.

What the unit enforces, so you do not have to add it back:

- `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`,
  `NoNewPrivileges`, empty `CapabilityBoundingSet`/`AmbientCapabilities`;
- `ReadWritePaths=/var/lib/patchtrack` only — reports and the Solaris inbox;
- `RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX` (PostgreSQL over TCP or a
  local socket);
- `Restart=on-failure`, `RestartSec=5`, `TimeoutStopSec=30`.

Validate the units before enabling them with
`systemd-analyze verify deploy/systemd/*.service`.

SELinux notes for native mode: `/var/lib/patchtrack/reports` and
`/var/lib/patchtrack/solaris-inbox` need the standard `var_lib_t` type for a
systemd-managed service (default), and the Solaris collector account needs
write access via a group. Do not set the units to `permissive`.

## 6. Log rotation

Containers use the json-file driver with `max-size: 20m`, `max-file: 5`. For
native mode, systemd's journal handles rotation; set
`SystemMaxUse=2G` in `/etc/systemd/journald.conf`.

Application logs never contain credentials; if you find one, treat it as a
defect and report it.

## 7. Restart and maintenance

```bash
cd /opt/patchtrack
docker compose restart gateway            # after a config change to the gateway only
docker compose up -d --force-recreate     # after changing .env
docker compose stop grafana               # safe at any time
```

Maintenance that requires downtime: schema migrations, a database restore, and
changing `PATCHTRACK_SERVICE_TOKEN` (all services must agree).

## 8. Monitoring to configure

| Signal | Threshold |
|--------|-----------|
| `/readyz` per service | Non-200 for 2 minutes → page |
| `inventory.sync_runs` last success age | > 2× the configured interval → alert |
| Repeated `failed` sync runs | 3 in a row → alert |
| `compliance.asset_compliance` unknown+stale share | Sudden rise → investigate |
| Disk (`/var/lib/docker`, report volume) | > 80% |
| Backup job exit code | Non-zero → alert, and treat as no backup |
| `reporting.reports` stuck in `generating` | > 1 hour → investigate |

A Prometheus exporter is not included in this release; the checks above are
query-based (e.g. a scheduled `curl` + `psql` probe emitted to your existing
monitoring agent).

## 9. Escalation data to collect

Before raising an incident, gather:

```bash
cd /opt/patchtrack
docker compose ps
docker compose logs --since 1h --no-color > /tmp/patchtrack-logs.txt
docker compose exec -T postgres psql -U patchtrack -d patchtrack -c \
  "SELECT integration_key, status, started_at, error_summary
     FROM inventory.sync_runs ORDER BY started_at DESC LIMIT 10;"
docker compose exec -T postgres psql -U patchtrack -d patchtrack -c \
  "SELECT version, applied_at FROM shared.schema_migrations ORDER BY version;"
```

Attach those. Do **not** attach `.env`, credential files, or a full database
dump.
