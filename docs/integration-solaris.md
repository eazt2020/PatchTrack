# Integration: Solaris collection

## Role

Solaris hosts have no supported agent path in this environment and direct SSH
access is **not assumed**. Collection therefore uses an approved read-only
process: the organisation's existing Solaris tooling writes one JSON
assessment file per host into a drop location, and PatchTrack ingests it.

PatchTrack does not open an SSH connection to any host, does not install an
agent, and does not execute commands on Solaris.

## Two ingestion paths

### Path A — watched inbox (batch, preferred)

A directory mounted into the inventory container:

```
/var/lib/patchtrack/solaris-inbox/*.json
```

The Solaris job writes files here; a scheduled or manual `solaris` sync reads
every `*.json` file, parses it, and ingests it. Files are **left in place** —
PatchTrack does not delete or move them, because deleting evidence is not its
job. Housekeeping is the collection job's responsibility (for example, moving
processed files to its own archive).

Use a host path or named volume with the correct SELinux label (`:Z`) and make
it writable only by the collection account and readable by the container.

### Path B — API upload (interactive / scripted)

`POST /api/compliance/solaris/ingest` with a list of records. Requires
`sync:run`. Every upload is recorded in `compliance.solaris_uploads` with the
SHA-256 of the submitted payload (canonical JSON, sorted keys).

```json
[
  {
    "hostname": "solaris-db01",
    "fqdn": "solaris-db01.example.com",
    "ip_address": "10.20.0.11",
    "os_name": "Oracle Solaris",
    "os_version": "11.4",
    "assessed_at": "2026-10-25T02:00:00Z",
    "patches": [
      {
        "patch_id": "356890-01",
        "title": "SunOS 5.11: kernel update",
        "severity": "important",
        "released_at": "2026-09-02",
        "category": "kernel",
        "url": "https://support.oracle.com/…"
      }
    ]
  }
]
```

## Document contract

| Field | Required | Notes |
|-------|----------|-------|
| `hostname` | yes | Without it the record is rejected |
| `fqdn` | no | Used first for matching |
| `ip_address` | no | Used for matching only |
| `os_name` | no | Defaults to `Oracle Solaris` |
| `os_version` | no | |
| `assessed_at` | recommended | ISO 8601. If absent the ingest time is used — which makes freshness meaningless; always supply it |
| `patches` | yes (may be empty) | Must be a list |
| `patches[].patch_id` | yes | Missing it rejects the whole document |
| `patches[].severity` | no | critical/security → critical; important → important; recommended/moderate → moderate; low/optional → low; else `unknown` |
| `patches[].title`/`synopsis` | no | |
| `patches[].released_at` | no | Feeds remediation due dates |
| `patches[].url` | no | Stored as source reference |

An empty `patches` list is a valid, meaningful statement: the host has no
outstanding patches.

## Validation rules

- A file that is not valid JSON, not an object, or missing `hostname` or a
  `patch_id` causes a `SolarisIngestError`, and the **whole run fails** rather
  than silently ingesting a partial set of files. Fix the file and re-run.
- A record that parses but does not resolve to exactly one inventory asset is
  **rejected** and written to `inventory.reconciliation_conflicts` with reason
  `assessment_unmatched`. It is not guessed onto a "nearest" host.
- Inbox files are read with `json.loads` on UTF-8 text; nothing is executed.

## Matching to inventory

For each Solaris record:

1. exact FQDN (case-insensitive), then
2. exact hostname (case-insensitive), then
3. exact IP.

A match must be **unique**. Two assets sharing the hostname (common when a
Solaris host has both a physical and a logical identity) yields a conflict and
a rejected record. Resolve it in AssetExplorer or via the reconciliation view.

## Why not SSH

The requirement is explicit that direct SSH collection must not be assumed.
Reasons that hold in practice:

- opening an inbound path from a central tool to every Solaris host widens the
  attack surface and usually cannot be approved;
- credentials for hundreds of hosts would have to be stored centrally;
- the existing Solaris team already has an approved collection mechanism.

If the organisation later approves an SSH-based collector, it should write the
same JSON documents — the ingestion contract is the boundary and does not need
to change.

## Failure behaviour

| Condition | Result |
|-----------|--------|
| Inbox directory missing | No files; run reports zero records (not an error) |
| One malformed file | Run `failed` with the filename in the error; no partial ingest |
| Record does not match inventory | Record rejected, conflict recorded, run `partial` |
| Source link already established | Re-ingest updates the same asset |

## Operational notes

- Schedule the Solaris sync after the collection job writes its files, with
  enough margin that a slow host does not produce a half-complete batch. The
  fail-whole-run behaviour is deliberate: a partially ingested batch is harder
  to reason about than no batch at all.
- Keep `freshness_hours` at least twice the collection interval (weekly job →
  192 hours), or Solaris hosts will oscillate between Stale and Compliant.
- Monitor `compliance.solaris_uploads` for unexpected rejections; a sudden run
  of `rejected` rows is usually an AssetExplorer field change breaking the
  hostname match.
