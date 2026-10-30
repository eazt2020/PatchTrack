# Integration: Solaris collection

## Role

PatchTrack collects Solaris patch state directly from each server. There is no
centralised configuration manager to depend on, so each host is a target in
PatchTrack with its own address, credential reference, pinned SSH host key and
Solaris major version. A sync contacts every enabled target concurrently and
writes one assessment per host; one unreachable host does not stop the others.

The collector is a fixed, read-only command. PatchTrack never opens an
interactive shell, never forwards ports, never runs commands supplied by the
target and never installs anything.

## Two Solaris generations, two honest models

| Host | What the collector can see | How PatchTrack assesses it |
|------|----------------------------|----------------------------|
| Solaris 11.4+ | The installed `entire` version and the newest `entire` version in the IPS catalogue | The latest SRU is a pending finding when it is newer than the installed one. Oracle does not publish severity or CVE data in IPS metadata, so findings are `unknown` severity unless the organisation classifies them. |
| Solaris 10 | Only the patches that are *installed* — there is no local catalogue of available patches | PatchTrack diffs installed revisions against the **Solaris 10 baseline** assigned to the target. A patch is satisfied when the installed revision is at least the required revision. |

A Solaris 10 target with **no baseline** produces no assessment and a target
error. It is Unknown, never "fully patched". This is deliberate: an empty
patch list from a host that cannot enumerate available patches is not evidence
of compliance.

`smpatch` and Oracle Update Manager are not used. They depend on the Oracle
update service and credentials that are frequently absent on isolated
Solaris 10 estates.

## Host requirements

1. An unprivileged account able to run the two read-only commands the collector
   needs (`pkg list` for 11, `showrev -p` for 10). No sudo is required.
2. An unencrypted OpenSSH private key for that account. The key is stored as a
   PatchTrack secret reference (`file:` or `env:`), never in the database.
3. The pinned SHA-256 host key fingerprint recorded from the server's console
   or a trusted first connection. A blank or mismatched fingerprint is an
   error.
4. The collector installed at `/opt/patchtrack/collect-solaris` (source:
   `deploy/solaris/collect-solaris`).

Forced command, key-only authentication and `restrict` keep the account able
to do exactly one thing:

```
# /etc/ssh/sshd_config.d/patchtrack.conf
Match User patchtrack
    PasswordAuthentication no
    PubkeyAuthentication yes
    PermitTTY no
    X11Forwarding no
    AllowTcpForwarding no
    PermitTunnel no
    PermitUserEnvironment no
    ForceCommand /opt/patchtrack/collect-solaris --format=json
```

Restart `sshd` after validating the configuration with `sshd -t`. The account
needs a locked password (`passwd -l patchtrack`).

## Collector contract

The collector prints exactly one JSON object to stdout.

Solaris 11.4+:

```json
{
  "hostname": "solaris-db01",
  "fqdn": "solaris-db01",
  "os_name": "Oracle Solaris",
  "os_version": "11.4",
  "zone": "global",
  "assessed_at": "2026-10-25T02:00:00Z",
  "collector_version": 1,
  "patches": [
    {"patch_id": "SOLARIS-11.4-SRU-48", "title": "Solaris 11.4 SRU 48", "category": "update"}
  ]
}
```

Solaris 10:

```json
{
  "hostname": "solaris-app01",
  "fqdn": "solaris-app01",
  "os_name": "Oracle Solaris",
  "os_version": "10",
  "zone": "global",
  "assessed_at": "2026-10-25T02:00:00Z",
  "collector_version": 1,
  "installed_patches": [
    {"patch_id": "147147", "revision": "26"},
    {"patch_id": "148888", "revision": "14"}
  ]
}
```

A malformed document, a non-zero exit or an unreachable host is a collection
failure for that target. It never becomes a zero-finding assessment. For
Solaris 11, an unreadable IPS catalogue also exits non-zero (`3`), because
"cannot determine" must not be reported as "no updates".

## Configuring targets in PatchTrack

Open **Integrations → Solaris → Manage hosts**. Each target records:

| Field | Meaning |
|-------|---------|
| Name | Stable identity. It becomes the source reference (`solaris:<name>`) and survives host renames. |
| Hostname / port | SSH endpoint |
| Username | Forced-command account |
| Private key reference | `file:/etc/patchtrack/secrets/<host>.key` or `env:<NAME>` |
| Pinned host key | `SHA256:…`. Leaving it blank makes the first test fail and report the actual fingerprint. |
| Solaris version | 10 or 11 |
| Zone | `global` or a non-global zone name, recorded on the target |
| Baseline | Required for Solaris 10; ignored for 11 |
| Collector command | Optional per-target override; must be an absolute path |

**Test** probes one saved target: SSH, host key, authentication, collector
output and baseline presence. It writes no assessment. A full sync is
**Run now** (or the scheduler).

The card's **Configure** action controls the integration itself: display name,
enabled flag, sync interval, the default collector command, SSH concurrency and
the fallback inbox path. Hosts, keys and baselines remain under **Manage
hosts**.

## Solaris 10 baselines

A baseline is an administrator-supplied list of the patches that must be
installed, validated on upload:

```json
[
  {"patch_id": "147147-26", "title": "Kernel update", "severity": "critical"},
  {"patch_id": "148888", "min_revision": 12, "title": "SSH", "obsoletes": ["148000"]}
]
```

- `patch_id` may carry the required revision (`147147-26`). When `min_revision`
  is also supplied, `min_revision` wins.
- `obsoletes` lists older patches that a satisfied entry replaces. An obsolete
  patch is not reported as missing when the replacing entry is installed.
- `severity` is one of critical, important, moderate, low. Anything else is
  stored as `unknown`; PatchTrack does not invent a severity Oracle never
  published.
- Duplicate patch ids and more than 50,000 entries are rejected.
- A baseline's Solaris major version must match the target's.

Baselines are named and versioned by name, not mutated implicitly: create a
new baseline for a new quarter and repoint targets at it. The old baseline
remains available for the audit trail.

## Ingestion fallback: watched inbox / API upload

SSH targets take precedence. When no SSH targets are configured, the `solaris`
integration still accepts the original batch model:

- `/var/lib/patchtrack/solaris-inbox/*.json` — one document per host using the
  `patches` contract above (files are left in place), or
- `POST /api/compliance/solaris/ingest` with a list of documents (`sync:run`).
  Each upload is recorded in `compliance.solaris_uploads` with the SHA-256 of
  the canonical payload.

This path remains useful for hosts that cannot be reached from PatchTrack or
during a transition. Once SSH targets exist, the inbox is no longer read: two
sources writing the same host would fight over its assessment.

## Matching to inventory

Each assessment is linked to exactly one asset:

1. the stable source link (`solaris:<target name>`), then
2. exact FQDN, then exact hostname, then exact IP.

A match must be unique. Anything else is rejected into
`inventory.reconciliation_conflicts` with reason `assessment_unmatched`; it is
never guessed onto a nearby host.

## Failure behaviour

| Condition | Result |
|-----------|--------|
| One target unreachable | That target records `last_error`; the run is `partial`; other targets are ingested |
| Every target fails | Run `failed`; no assessments; no inventory is deleted |
| Solaris 10 target without a baseline | Target error (unless other targets succeed, then `partial`) |
| Baseline entry invalid | Target error naming the baseline and entry |
| Host key missing or mismatched | Target error; no connection accepted |
| Collector prints malformed JSON | Target error |
| Collector exits non-zero | Target error including its stderr |
| Inbox file malformed (fallback mode) | Whole run `failed`; no partial ingest |

A failed collection never classifies a host as compliant and never removes its
previous assessment.

## Secrets

`secret_ref` is a reference, not a key. The API rejects anything that does not
start with `env:` or `file:`. Private keys must be unencrypted OpenSSH keys;
passphrase-protected keys are refused rather than prompting. Mount key files
read-only and restrict them to the service account.

## Operational notes

- Schedule the sync at least as often as the freshness threshold expects. A
  every-6-hours sync with a 48-hour freshness threshold gives room for two
  missed runs before hosts go Stale.
- Keep `freshness_hours` at least twice the collection interval.
- Use one target per non-global zone only when the zone has its own patch
  state that matters; otherwise the global zone's assessment is the host's.
- Review `last_error` on the target list after upgrades: a changed host key is
  the most common failure and is reported with both fingerprints.
