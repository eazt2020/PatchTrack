# Integration: ManagedEngine AssetExplorer

## Role

AssetExplorer is the **authoritative inventory source**. Every server that
PatchTrack reports on must exist in AssetExplorer first. Patch management
systems can only attach assessments to assets that AssetExplorer has
introduced.

## Direction and access

- **Pull, outbound only.** PatchTrack makes HTTPS `GET` requests to the
  AssetExplorer REST API. AssetExplorer never calls PatchTrack.
- **Read-only.** PatchTrack never creates, updates or deletes an AssetExplorer
  record.
- **No SSH, no agent.** Network access is one TCP/443 path from the inventory
  container to the AssetExplorer API endpoint.

```
inventory container ──HTTPS GET──▶ AssetExplorer API :443
```

## Endpoint and authentication

| Setting | Value |
|---------|-------|
| Endpoint | `https://assetexplorer.example.com` (no trailing `/api`) |
| Path used | `GET /api/v3/asset_servers?input_data=<url-encoded JSON>` |
| API build | AssetExplorer build 7700 or later (the documented V3 Assets API) |
| Auth methods | `integration_key` → `AUTHORIZATION: <key>` (on-premise default); `bearer` → `Authorization: Bearer <token>`; `apikey` → `X-API-Key: <key>` |
| Credential | `secret_ref` reference, e.g. `env:PATCHTRACK_ASSETEXPLORER_TOKEN` or `file:/etc/patchtrack/assetexplorer.token` |
| TLS | `verify_tls=true`; the issuing CA must be trusted by the container |
| Timeouts | `connect_timeout_s` (default 30), `read_timeout_s` (default 60) |
| Page size | `page_size` (default 200; AssetExplorer caps one page at 100) |

The integration key is created under **Admin → Developer Space → Integration
Key** (role required: SDAdmin). It is sent as the raw value of the
`AUTHORIZATION` header, not as a `Bearer` token.

Older builds expose `/api/v3/assets` with the rows under `assets`. Select that
path with `extra.api_path`; ManageEngine supports it only until April 2026:

```json
{ "extra": { "api_path": "/api/v3/assets" } }
```

The connector also sends
`Accept: application/vnd.manageengine.sdp.v3+json`, the documented media type
for the V3 API.

## Request shape

Paging is controlled inside `input_data`, not by `page`/`size` query
parameters:

```
GET /api/v3/asset_servers?input_data=%7B%22list_info%22%3A%7B%22start_index%22%3A1%2C%22row_count%22%3A2%7D%7D
```

which decodes to:

```json
{"list_info": {"start_index": 1, "row_count": 2}}
```

## Response shape expected

```json
{
  "response_status": { "status_code": 2000, "status": "success" },
  "list_info": {
    "has_more_rows": true,
    "start_index": 1,
    "row_count": 2,
    "sort_field": "id",
    "sort_order": "asc"
  },
  "asset_servers": [
    {
      "id": "4101",
      "name": "srv-dc-01",
      "fqdn": "srv-dc-01.corp.example",
      "ip_address": "10.20.0.11",
      "module": { "id": "3", "name": "Server", "display_name": "Server" },
      "product": { "id": "77", "name": "PowerEdge R650" },
      "os_name": "Red Hat Enterprise Linux",
      "os_version": "9.4",
      "asset_status": "In Use",
      "environment": "Production",
      "business_group": "Infrastructure",
      "application_group": "Directory",
      "asset_owner": "a.singh",
      "department": "IT",
      "location": "DC1",
      "lifecycle_status": "Active",
      "asset_classification": "Tier 1",
      "created_time": "2024-01-02 09:15:00",
      "last_modified_time": "2025-06-30 11:02:00"
    }
  ]
}
```

The row list key is `asset_servers`; `asset_assets` and the older `assets` key
are also accepted. Pagination advances `start_index` by `page_size` while
`list_info.has_more_rows` is true. When `list_info` is absent, a page shorter
than `page_size` ends collection. A hard cap of 10,000 pages raises an error
rather than looping forever. A `response_status` whose status is not
`success` is raised as a probe/collection failure with ManageEngine's message.

## Field mapping

Installations customise AssetExplorer fields. Override any mapping through the
integration's `extra.field_map`:

```json
{
  "secret_ref": "env:PATCHTRACK_ASSETEXPLORER_TOKEN",
  "endpoint": "https://assetexplorer.example.com",
  "enabled": true,
  "extra": {
    "field_map": {
      "hostname": "name",
      "business_group": "business_service",
      "environment": "operational_status"
    }
  }
}
```

Keys that can be remapped: `source_id`, `hostname`, `fqdn`, `ip_address`,
`os_name`, `os_version`, `asset_type`, `asset_status`, `environment`,
`business_group`, `application_group`, `asset_owner`, `department`,
`location`, `lifecycle_status`, `classification`, `source_created_at`,
`source_modified_at`.

## Normalisation rules

| Field | Rule |
|-------|------|
| `source_id` | Required. Records without it are skipped with a warning and counted as rejected. |
| `hostname` | Required. Trimmed. Records without it are skipped. |
| `os_family` | Derived, never taken from the source: `windows` if the OS name contains "windows"; `solaris` for Solaris/SunOS; `rhel` for RHEL, CentOS, Rocky, Oracle Linux, AlmaLinux, Fedora, generic "linux"; otherwise `other`. |
| `lifecycle_status` | Mapped case-insensitively: active/in use/in-use/staging → `active`; retired → `retired`; decommissioned/disposed → `decommissioned`; anything else → `unknown`. |
| Missing attributes | Stored as the empty string or `UNKNOWN`. **Never** inferred from a neighbouring field. |
| Duplicate `source_id` in one response | First wins, later duplicate logged and skipped. |

## Identity and reconciliation

`assetexplorer_id` is the immutable key. On every run:

1. If the id exists → update the mutable fields, refresh `last_synced_at`.
2. If not → insert a new asset.
3. If the id is new but the hostname/FQDN/IP is already known → the alias
   table resolves it to the existing asset and records the new identifier,
   rather than creating a duplicate.

An asset that disappears from the response is **not** deleted and **not**
retired. Its `last_synced_at` stops advancing, and its assessments go Stale
once the freshness threshold passes. Retirement happens only when
AssetExplorer says so.

### Local placement overrides

Environment and business service can be corrected locally for one asset
(**Server inventory → Edit**) when AssetExplorer is late or wrong. The override
is stored in `inventory.asset_overrides`, a table this connector never reads or
writes. Every run continues to update `inventory.assets.environment` and
`business_group` with the source values; the override is applied on read through
`inventory.asset_effective`. Operations do not need to pause, reconfigure or
test the integration, and the override is not written back to AssetExplorer.
The source value is never changed by PatchTrack, and both values remain
visible in the UI.

The values offered when an operator makes a correction are maintained by an
administrator in Settings (`inventory.placement_options`). That table is
likewise independent of this connector: a source value that does not appear in
the list is still ingested and shown. The list only constrains what an operator
may type, not what AssetExplorer may report.

Renaming a list entry does not write anything back here. The old spelling is
recorded in `inventory.placement_aliases` and resolved on read, so every screen
shows the new name while `inventory.assets` keeps the value AssetExplorer
reported. A later run that still reports the old spelling therefore cannot undo
the rename; correcting it in AssetExplorer removes the need for the alias, and
the alias remains harmless if it is not corrected.

## Failure behaviour

| Condition | Result |
|-----------|--------|
| Connection error / timeout | Sync run `failed`; no asset row touched; error summary recorded |
| HTTP 401/403 | `failed`, "authentication failed" |
| HTTP 4xx (other) | `failed`, request rejected |
| HTTP 5xx | `failed`, server error |
| Malformed JSON, or `data` not a list | `failed`, unexpected payload |
| One record fails to normalise | Record rejected, run becomes `partial`; other records proceed |

A failed AssetExplorer run never causes patch assessments to be discarded, and
never marks anything compliant.

## Health states

`GET /api/inventory/integrations` reports:

| State | Meaning |
|-------|---------|
| `not_configured` | No endpoint |
| `disabled` | Endpoint set, `enabled=false` |
| `pending` | Enabled, never run |
| `authentication_failure` | Last run failed with an auth error |
| `unavailable` | Last run failed otherwise |
| `degraded` | Last run `partial` |
| `stale` | Last success older than twice `sync_interval_minutes` |
| `healthy` | Last success within twice the interval |

## Configuring it

Put the credential in the deployment's secret store and reference it — never
paste it into the API. Either add it to `/etc/patchtrack/.env` (mode 0600):

```bash
PATCHTRACK_ASSETEXPLORER_TOKEN=<token from the AssetExplorer API page>
```

or write it to a file and reference that:

```bash
install -m 0600 /dev/null /etc/patchtrack/assetexplorer.token
printf '%s\n' '<token>' > /etc/patchtrack/assetexplorer.token
```

Then configure the integration through the UI (Administration → Integrations)
or the API, using an administrator session with `integrations:manage`:

```http
PATCH /api/inventory/integrations/assetexplorer
Content-Type: application/json

{
  "endpoint": "https://assetexplorer.example.com",
  "auth_method": "integration_key",
  "secret_ref": "env:PATCHTRACK_ASSETEXPLORER_TOKEN",
  "enabled": true,
  "sync_interval_minutes": 360,
  "verify_tls": true
}
```

Test the configuration first — this exercises the endpoint, TLS and
integration key and writes nothing:

```http
POST /api/inventory/integrations/assetexplorer/test
Content-Type: application/json

{}
```

A successful response is `{"status": "success", "detail": "AssetExplorer
answered with HTTP 200; …", "duration_ms": …}`; a failure returns
`{"status": "failed", "detail": "<reason>"}` with HTTP 200, so the UI can show
the reason. The same route accepts draft `endpoint`, `auth_method`,
`secret_ref`, `verify_tls`, timeout and `page_size` values to test a change
before saving it.

Then verify with a collection:

```http
POST /api/inventory/sync   {"integration": "assetexplorer"}
GET  /api/inventory/sync-runs?integration=assetexplorer
```

A `success` run with `records_created` matching the expected estate size is the
acceptance test.

## Verification against a real installation

The endpoint, authentication header, `input_data` paging and response envelope
follow ManageEngine's published V3 API (build 7700+). They have been exercised
against a simulator that reproduces that documented shape, not against a live
installation. Custom field names still differ per installation. Before
enabling in production:

1. Call the endpoint once with `curl` and the same credential, capture one
   record.
2. Compare the keys with `DEFAULT_FIELD_MAP` (`id` and `name` are the V3
   resource's own identifiers).
3. Set `extra.field_map` for any difference.
4. Run a sync and inspect `GET /api/inventory/assets?page_size=5`.

Do not enable the integration until a small sync produces the expected asset
count and correct environment/business-group values.
