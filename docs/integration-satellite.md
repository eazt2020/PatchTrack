# Integration: Red Hat Satellite

## Role

Satellite is the authoritative patch source for RHEL hosts. PatchTrack reads
the **errata a host has not yet applied**. Applied errata are not findings;
PatchTrack does not re-derive patch state from package versions.

## Direction and access

- **Pull, outbound only.** HTTPS `GET` to the Satellite REST API v2.
- **Read-only.** PatchTrack never applies errata, never schedules a task and
  never changes a host's content view.
- **No SSH.** Nothing connects to a managed host.
- One TCP/443 path from the inventory container to the Satellite server.

```
inventory container ──HTTPS GET /api/v2──▶ Satellite :443
```

## Endpoint and authentication

| Setting | Value |
|---------|-------|
| Endpoint | `https://satellite.example.com` (the connector appends `/api/v2`) |
| Auth | HTTP Basic: `extra.username` + the secret from `secret_ref` |
| Credential | `secret_ref`, e.g. `env:PATCHTRACK_SATELLITE_PASSWORD` or `file:/etc/patchtrack/satellite.password` |
| TLS | `verify_tls=true`; install the Satellite CA on the host |
| Page size | `page_size` (default 100) |
| Timeout | `read_timeout_s` (default 60) |

The service account needs only read access to hosts and errata. A least-privilege
role with `view_hosts` and `view_errata` is sufficient; do not use the
Satellite administrator account.

## Calls made

1. `GET /api/v2/hosts?search=subscription_status = valid&page=N&per_page=M`
   — paginated until `len(items) >= total`.
2. For each host: `GET /api/v2/hosts/{id}/errata`
   with `search=errata_type = security or errata_type = bugfix or errata_type = enhancement`.

Hosts without an `id` or `name` are skipped. Hosts with no errata produce a
**complete assessment with zero findings** — that is a real "this host is
patched" statement, not an absence of data.

## Normalisation

| Source field | PatchTrack |
|--------------|------------|
| `host.id` | `source_ref` → `inventory.asset_source_links.source_ref` (stable link) |
| `host.name` | `fqdn`; `hostname` is the first label |
| `host.ip` | `ip_address` for matching only |
| `host.operatingsystem_name` | `os_name` |
| `host.content_facet_attributes.content_view_version` | `os_version` |
| `host.updated_at` | `assessed_at` |
| errata `errata_id` | `findings.advisory_id` |
| errata `severity` | mapped: critical, important, moderate, low pass through; `security` → important; `bugfix` → moderate; `enhancement` → low; anything else → `unknown` |
| errata `title`/`summary` | finding title |
| errata `issued`/`updated_at` | `released_at` (feeds remediation due dates) |

`assessed_at` is Satellite's `updated_at`. If Satellite has not updated a
host in longer than the policy's `freshness_hours`, that host will be
**Stale** even though collection is current. That is the intended behaviour:
the freshness question is about the evidence, not about the collector.

## Matching to inventory

In order, and stopping at the first unambiguous result:

1. `inventory.asset_source_links` on `('satellite', host.id)` — the stable link,
   established on the first successful match.
2. Exact FQDN, case-insensitive (current asset or alias).
3. Exact hostname, case-insensitive (current asset or alias).
4. Exact IP (current asset).

If a key matches **more than one** asset, the assessment is refused and an
`ambiguous_match` conflict is written. It is never assigned to the first row
returned by the database.

An unmatched host is counted in `sync_runs.assets_unmatched`, listed in the
reconciliation view, and produces no assessment. It does not become an implicit
Unknown asset — PatchTrack does not invent inventory.

## Failure behaviour

| Condition | Result |
|-----------|--------|
| Timeout / connection error | Sync run `failed`; no assessment written |
| 401/403 | `failed`, "Satellite authentication failed" |
| Other 4xx/5xx | `failed` with the status code |
| Malformed JSON | `failed` |
| Reconciliation ambiguous/unmatched | Assessment skipped; run `partial`; conflict recorded |

The critical property: **a failed Satellite collection writes no assessment.**
Existing assessments keep their original `assessed_at` and therefore age into
Stale. A collection outage degrades the reported compliance; it never improves
it.

## Configuring it

```http
PATCH /api/inventory/integrations/satellite
{
  "endpoint": "https://satellite.example.com",
  "auth_method": "basic",
  "secret_ref": "env:PATCHTRACK_SATELLITE_PASSWORD",
  "enabled": true,
  "sync_interval_minutes": 360,
  "verify_tls": true,
  "extra": { "username": "patchtrack-readonly" }
}
```

The username lives in `extra` (not sensitive); the password is only ever in the
referenced environment variable or file.

## Sizing and performance

One errata request per host. For an estate of N RHEL hosts the run makes
roughly `1 + N` API calls, serial. At 60 s timeout and 200 hosts this is
minutes, not seconds.

Guidance:

- Run Satellite collection hourly to every few hours; it is the most granular
  source.
- Keep `page_size` at or below the Satellite maximum for the version.
- If the estate exceeds a few thousand hosts, schedule the sync outside
  business hours and raise `read_timeout_s`.

## Verification checklist

1. `curl -u user:pass https://satellite.example.com/api/v2/hosts?per_page=1`
   succeeds from **inside the inventory container** (network and CA proven).
2. Manual `POST /api/inventory/sync {"integration":"satellite"}` returns
   `status: success` or `partial`.
3. `GET /api/inventory/sync-runs?integration=satellite` shows
   `records_seen` close to the expected host count.
4. `GET /api/compliance/summary` shows the RHEL population moving from Unknown
   to Compliant/Non-compliant.
5. Reconciliation view is empty or contains only explained conflicts.
