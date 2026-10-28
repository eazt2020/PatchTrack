# Integration: Microsoft Configuration Manager (SCCM)

## Role

Configuration Manager is the authoritative patch source for Windows Server
hosts. PatchTrack reads the **software updates a device is missing**, from a
read-only view. It never deploys, approves or expires an update.

## Two supported access paths

SCCM installations differ in what they expose. Choose one and record the choice
in the integration configuration.

### Path A — Administration Service (REST), preferred

| Setting | Value |
|---------|-------|
| Endpoint | `https://sccm.example.com` (the connector appends `/SMS_REST/DeviceCompliance`) |
| Auth | Bearer token from the organisation's approved credential process (`secret_ref`) |
| TLS | `verify_tls=true`; SCCM's certificate must be issued by a CA the container trusts |
| Page size | `page_size` (default 200) via `$top` |
| Pagination | `$skip` until a short page is returned (hard cap 1,000,000 rows) |

The endpoint must be a **read-only view** — a custom WMI/SQL view exposed by
the Administration Service, or a purpose-built REST facade — not the full
SMS Provider. PatchTrack requires:

```json
{
  "value": [
    {
      "ResourceID": 16777220,
      "Name": "win-app-01.example.com",
      "IPAddresses": ["10.30.4.21"],
      "OperatingSystemNameAndVersion": "Microsoft Windows Server 2022",
      "OSVersion": "10.0.20348",
      "LastDDR": "2026-10-25T21:00:00Z",
      "AssessmentComplete": true,
      "MissingUpdates": [
        {
          "ArticleID": "5041234",
          "Title": "2026-10 Cumulative Update for Windows Server 2022",
          "Severity": "Critical",
          "Classification": "Security Updates",
          "DateReleased": "2026-10-08T00:00:00Z",
          "CI_ID": 16790001
        }
      ]
    }
  ]
}
```

### Path B — approved export

Where no safe REST surface exists, the organisation's data pipeline writes the
same JSON document to a location the inventory service can read. This is an
operational arrangement, not a code path: the connector's input contract is
identical, so either the export is served by a small read-only HTTP endpoint
(configure Path A against it) or it is ingested by the same normaliser in an
approved script.

Do **not** give PatchTrack a SQL login to the Configuration Manager database.
The SCCM site database is a write-capable operational store; a reporting view
or an export is the correct boundary.

## Normalisation

| Source | PatchTrack |
|--------|------------|
| `ResourceID` / `ResourceId` | `source_ref` (`sccm:device/<id>`), stable link across renames |
| `Name` / `ResourceName` | `fqdn`; first label is `hostname` |
| `IPAddresses` (array) or `IPAddress` | `ip_address` for matching; the first value is used |
| `OperatingSystemNameAndVersion` | `os_name` (defaults to `Windows Server`) |
| `OSVersion` | `os_version` |
| `LastDDR` / `LastAssessmentTime` | `assessed_at` |
| `AssessmentComplete` | `complete` when true, `partial` when false |
| update `ArticleID` / `BulletinID` / `ID` | `advisory_id` |
| update `Title` / `Name` | finding title |
| update `Severity` | critical/important/moderate/low pass through; `none`/`unspecified`/anything else → `unknown` |
| update `Classification` | finding category |
| update `DateReleased` / `DatePosted` | `released_at` |

Records without a name or a resource id are skipped and logged. An update
object that is not an object is dropped rather than crashing the run.

`assessment_status = partial` still produces findings, but the freshness rule
applies to `assessed_at` as usual. A device whose `AssessmentComplete` is false
and whose data is old will be Stale.

## Matching to inventory

Identical rules to Satellite (see
[integration-satellite.md](integration-satellite.md#matching-to-inventory)):
source link → exact FQDN → exact hostname → exact IP; ambiguity produces a
conflict, never a guess.

Windows hostnames are case-insensitive; matching uses `lower(...)` on both
sides.

## Important limitation

SCCM reports on devices it **manages**. A Windows server that is in
AssetExplorer but not in Configuration Manager produces no assessment and is
therefore Unknown. That is correct and should be surfaced to the Windows
operations team — it usually means a missing client, a broken boundary or a
device in an unmanaged workgroup. Do not "fix" it by excluding the host.

## Failure behaviour

| Condition | Result |
|-----------|--------|
| Timeout / connection error | Sync run `failed`, no assessments written |
| 401/403 | `failed`, "SCCM authentication failed" |
| Other HTTP error | `failed` with the status |
| Malformed JSON, missing `value` | `failed` |
| One device unparseable | Device skipped and logged; run `partial` |

## Configuring it

```http
PATCH /api/inventory/integrations/sccm
{
  "endpoint": "https://sccm-reporting.example.com",
  "auth_method": "bearer",
  "secret_ref": "env:PATCHTRACK_SCCM_TOKEN",
  "enabled": true,
  "sync_interval_minutes": 720,
  "verify_tls": true
}
```

## Verification checklist

1. From inside the inventory container, fetch one page of the endpoint with the
   same token and confirm the shape matches the contract above.
2. Manual sync; expect `status: success` (or `partial` with an explained
   unmatched count).
3. Confirm the Windows population moves out of Unknown in
   `/api/compliance/summary`.
4. Confirm the Windows environment/business-group breakdown is populated — if
   every device shows `UNKNOWN`, AssetExplorer is not supplying those fields
   for Windows hosts; fix the AssetExplorer field map, do not patch it here.
