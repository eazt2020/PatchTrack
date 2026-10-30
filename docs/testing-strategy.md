# Testing strategy

## 1. What is verified, and how

The suite has three layers, plus two verification activities that are not
pytest tests.

| Layer | Location | Needs | Count |
|-------|----------|-------|-------|
| Unit tests | `tests/unit/` | Nothing but Python | 481 |
| Database integration tests | `tests/integration/` | PostgreSQL | 145 |
| Browser smoke test | `tests/ui/smoke.py` | Playwright + Chromium + a running stack | 1 script, 14 routes |
| Dashboard verification | manual procedure (§7) | Grafana + PostgreSQL | 15 panels |
| Deployment verification | manual procedure (§8) | Docker + RHEL/Rocky 9 | full stack |

Run:

```bash
/opt/patchtrack-venv/bin/python -m pytest                       # 481 pass, 145 skip without a DB
PATCHTRACK_TEST_DATABASE_URL='postgresql+psycopg://patchtrack@/patchtrack_test?host=/tmp' \
  /opt/patchtrack-venv/bin/python -m pytest                     # 626 pass
```

`pytest.ini` sets `testpaths = tests`, `--strict-markers` and fails on an
unknown mark. Integration tests skip themselves when no database is reachable,
so the same command works on a build host without PostgreSQL.

## 2. Unit tests

| File | Tests | What it pins |
|------|-------|--------------|
| `test_engine.py` | 44 | Every one of the five states; the state ordering; empty population yields `None`; the freshness boundary is inclusive at exactly the threshold and stale just past it; scope rules and first-match-wins; excluded assets stay out of the denominator; patch counts are never blended; remediation due dates; `period_bounds` for months, quarters, year rollover and invalid input |
| `test_period_parity.py` | 27 | `compliance.app.engine.period_bounds` and `reporting.app.periods.period_bounds` return identical results for the same input, and both reject the same invalid inputs. A report and a snapshot must never disagree about which days a period covers. |
| `test_secrets.py` | 15 | Reference forms (`env:`, `file:`, empty); a bare credential is rejected; resolution from environment and file |
| `test_security.py` | 32 | The full role matrix; administrator-only capabilities withheld from other roles; token round-trip; forged, expired, unsigned and missing-claim tokens rejected; gateway header trust requires the service token; the forced-rotation claim survives encode/decode; the revocation counter travels in the token |
| `test_report_pdf.py` | 18 | A valid PDF is produced; EOF marker present; non-trivial size; truncation rejected; non-PDF rejected; tiny file rejected; an empty population renders without inventing 0%; a cover logo is embedded and a corrupt logo is skipped without failing the report; group names, hostnames, branding and methodology text are XML-escaped |
| `test_request_guards.py` | 5 | Declared and streamed bodies over the limit are refused with 413; a body within the limit reaches the route; a `RecursionError` is answered with 422 rather than 500; the guard installs on a FastAPI app |
| `test_scheduler.py` | 12 | Report schedule maths: monthly/quarterly advancement; a passed run time moves to the next period; the result is always in the future; month-end clamping (31st → 28 Feb); timezone changes the instant; unknown timezone falls back to UTC; DST handled on the local wall clock |
| `test_inapp_scheduler.py` | 8 | Only an explicit `status: failed` result counts as failure; a disabled scheduler registers no handlers; installing registers startup and shutdown and keeps the job list |
| `test_gateway_rotation.py` | 11 | A session that still owes a password change is refused (403) by every proxied route, including identity administration; `/api/me` reports the flag; a normal session is not caught by the guard; encoded and double-encoded dot segments are refused with 400 |
| `test_gateway_version.py` | 2 | `/api/version` and the FastAPI metadata both report the release from `PATCHTRACK_VERSION`, so the console and the API schema cannot display a hardcoded version |
| `test_net.py` | 4 | Client addresses are canonicalised; header text, a list, and a CIDR are not addresses and become NULL rather than reaching an `inet` column |
| `test_client_address.py` | 4 | The gateway ignores a browser-supplied `X-Forwarded-For`, uses the ASGI peer, and records NULL when the peer is not an address |
| `test_container_entrypoint.py` | 6 | A command supplied by compose (the one-shot `migrate` job) is executed by the entrypoint rather than swallowed; a service start without `PATCHTRACK_SERVICE` still fails loudly; the command branch precedes the service check; the log level is lowercased for uvicorn; the build elevates for its ownership step and ends as uid 1001 |
| `test_image_requirements.py` | 2 | Every third-party import in `services/` resolves to a distribution declared in `deploy/container/requirements.txt`, and `email-validator` is present for pydantic's `EmailStr` |
| `test_systemd_units.py` | 14 | Each instance loads its own port file; no built-in port; migrations are a required predecessor unit; non-root, no capabilities, `ProtectSystem=strict`, `ReadWritePaths` limited to `/var/lib/patchtrack`; `--forwarded-allow-ips` is configurable and never `*`; no scheduler timer or tick unit is shipped |
| `test_migrations.py` | 6 | The migration directory is found in both the source-tree layout and the image layout (`/opt/patchtrack/common/patchtrack_common` is one level shallower), the nearest ancestor wins, and a missing directory raises instead of reporting an empty schema as up to date |
| `test_cookie_policy.py` | 10 | Secure cookies are required for every non-loopback host by default; `always` and `never` behave as documented; an IPv6 loopback literal is recognised; `never` with `PRODUCTION` is refused at startup; an unknown policy value is rejected |
| `test_server_sort.py` | 6 | The server list resolves a sort key through a fixed column table; an unknown key is a 422 rather than a silently ignored sort; rows with no value sort last in both directions; a hostname/id tie-break keeps paging stable |
| `test_trend_periods.py` | 13 | Monthly, quarterly and yearly windows start on a real calendar period boundary; shifting back by N periods crosses year boundaries; labels read `Oct 26`, `Q4 2026`, `2026`; the basis table is the single source of truth for unit and default point count |
| `test_period_keys.py` | 28 | The dashboard's period keys resolve to exact calendar windows: month, quarter and year begin and end on the right days (including 29 February and 31 December); malformed, out-of-range and lower-case quarter keys are rejected rather than rounded; keys and reader labels round-trip |
| `test_seed_demo.py` | 5 | The five-year demo generator spans 60 months; every day's states add up to the in-scope population and its severity counts add up to the patch total; the monthly, quarterly and yearly series each contain both met and missed periods, plus a visible outage quarter; per-dimension group rows reconcile to the snapshot totals without exceeding any group's population; live hosts are assigned exactly one state |
| `test_job_scripts.py` | 8 | The manual job runner authenticates as the `scheduler` principal with the service token and uses no password, cookie or gateway route; it detects container versus native deployment, supports both Docker and Podman container runtimes, and the native path runs the interpreter with `-I` |
| `test_tls.py` | 12 | A first start generates a usable self-signed pair with the key mode 0600; the listener context enforces TLS 1.2; existing material is never regenerated by a restart and an incomplete pair is refused; mismatched, expired, passphrase-protected and malformed material is refused before anything is written; an uploaded pair becomes current and is served by the live context on the next handshake; only the gateway builds a context, and disabled TLS touches no files |
| `test_secret_files.py` | 13 | A `*_FILE` value supplies each supported credential and a required setting; the file wins over the environment while an explicit constructor value wins over both; only trailing line endings are removed, inner whitespace is kept; an empty file is an explicit empty value and an empty `_FILE` variable means unset; a missing file, a directory and an oversized file stop start-up without falling back; group/world-readable files are warned about without logging the secret; the opt-in Compose override mounts each application secret, masks the plain environment variables and uses the PostgreSQL password file |
| `test_serve.py` | 4 | The deployment launcher builds a TLS context only for the gateway with TLS enabled, generates the certificate on first launch, leaves other services and disabled deployments on plain HTTP, and defaults `--service` from `PATCHTRACK_SERVICE` |
| `test_uninstaller.py` | 16 | Reads the installation manifest, refuses a manifest from another directory, a symlinked manifest, an image that is not PatchTrack, overlapping paths, an unknown runtime and an unknown installation mode; accepts an air-gap manifest and treats one without a mode as online; refuses a bundle inside the application directory; removes only installer-created configuration and empty directories; preserves operator files; and recognises its own Docker proxy drop-in |
| `test_upgrade.py` | 9 | The guided upgrade validates target versions (no no-op, no downgrade, safe characters), edits `PATCHTRACK_VERSION` in place while preserving mode and unrelated lines, appends it when absent, reads quoted proxy values from `.env`, derives the loopback probe address from bind/port/TLS settings, refuses a backup directory inside the deployment, and places staging beside the installed tree |
| `test_installer.py` | 31 | The guided installer validates bind addresses, ports, certificate names and account names, rejecting email suffixes the identity service's `EmailStr` refuses; records the Docker, containerd and Podman data directories it created so rollback removes only those; refuses to install over its source tree or a non-empty target; renders an environment file with no bootstrap secrets; uses loopback for its own probe when published on all interfaces; reverses recorded actions in order; refuses to remove a protected system path; verifies the bootstrap administrator's authenticated session against a local HTTPS gateway, including the refusal when that session belongs to another account; validates proxy URLs, redacts proxy credentials, renders the service proxy environment and escapes systemd specifiers in the Docker proxy drop-in; renders a `patchtrack.service` that owns the Compose stack with the recorded install path and Docker or Podman runtime, and recognises only its own unit; and in air-gap mode verifies bundle checksums, requires the required archives to be listed while tolerating a removed optional archive, refuses a bundle overlapping the install directories, skips registry probes, loads and tags the supplied images, and skips the build; preflight output shows a `how to fix` line for failed and warning checks only |
| `test_integration_probe.py` | 11 | Each connector's Test configuration probe uses one page, succeeds on a documented response, reports 401/403 as an authentication failure, rejects a malformed payload and raises rather than silently returning an empty collection; the Solaris probe checks the inbox exists and every document parses |
| `test_backup_worker.py` | 10 | The archive worker's script parses and fails fast with a private umask; the dump excludes live session tables and the backup queue; the archive is published only after its digest is known; temporary files are excluded from the report archive and retention never follows a path out of the backup directory; the retention setting is bounded; an archive path outside the configured directory is refused; the `reporting.backup` job is registered |
| `test_run_log.py` | 7 | The install/upgrade/uninstall wrapper preserves the tool's exit status and captures stdout and stderr into one mode-0600 file; the directory is 0750 and `<tool>.log` links to the newest run; a failed log directory falls back to the user state directory with a warning; old timestamped logs are pruned while operator files are kept; a missing tool argument is refused; each wrapper invokes the logger with the right tool name and Python tool |

The unit tests are where the compliance rules are pinned. Any change to
`engine.py` that alters a published figure must change a test in
`test_engine.py`, which is the review trigger.

## 3. Integration tests

`tests/integration/test_reconciliation.py` — 13 tests,
`tests/integration/test_password_change.py` — 8 tests,
`tests/integration/test_rotation_end_to_end.py` — 3 tests,
`tests/integration/test_asset_overrides.py` — 5 tests,
`tests/integration/test_placement_options.py` — 10 tests,
`tests/integration/test_dashboard_breakdown.py` — 4 tests,
`tests/integration/test_dashboard_periods.py` — 12 tests,
`tests/integration/test_integration_test_endpoint.py` — 5 tests,
`tests/integration/test_inventory_stats.py` — 3 tests,
`tests/integration/test_justification_lifecycle.py` — 4 tests,
`tests/integration/test_report_branding.py` — 6 tests,
`tests/integration/test_report_deletion.py` — 5 tests,
`tests/integration/test_report_retention.py` — 6 tests,
`tests/integration/test_audit_retention.py` — 8 tests,
`tests/integration/test_session_revocation.py` — 10 tests,
`tests/integration/test_scheduler_state.py` — 8 tests,
`tests/integration/test_scheduler_config.py` — 6 tests,
`tests/integration/test_smtp_settings.py` — 13 tests,
`tests/integration/test_tls_settings.py` — 7 tests, and
`tests/integration/test_backup_archive.py` — 5 tests, all against a real,
freshly created database.

The fixture creates `patchtrack_test`, applies every migration, and drops it at
the end of the session. It **must not** be pointed at a database with data you
care about (guarded only by the database name).

Covered:

- create on first sight; rename in place (no duplicate asset);
- alias retention across renames;
- conflict raised when a source identifier moves to a different asset;
- no conflict on an identical re-sync (this test found the `inet::text` `/32`
  bug — see §9);
- match by source link, FQDN, hostname and IP;
- unmatched record produces no assessment;
- ambiguous match is refused rather than guessed;
- a failed sync leaves inventory and prior assessments intact;
- a duplicate source id is rejected;
- the audit table rejects `UPDATE` and `DELETE`.

`test_rotation_end_to_end.py` runs the gateway in-process against the identity
service on a real socket: sign in with a rotation-pending account, confirm the
403 on `/api/inventory/assets` and `/api/identity/users`, change the password,
and confirm the fresh session is unencumbered and the old password no longer
works.

Password rotation (`test_password_change.py`):

- changing the password to satisfy a forced rotation clears
  `must_change_password` and rotates the stored hash;
- a wrong current password is refused and changes nothing;
- the new password must differ from the old and meet the length minimum;
- every other refresh token is revoked, so a session opened with the old
  credential cannot be renewed;
- an unauthenticated caller cannot change a password, and a service-token
  caller with no user identity is refused cleanly.

Report branding (`test_report_branding.py`):

- the organisation name, logo and both signatories round-trip through the real
  settings row;
- a name-only save preserves a previously uploaded logo, because the update is
  partial by design;
- removing the logo requires `remove_logo`, and clearing it does not disturb the
  other branding fields;
- a data URI that is not PNG/JPEG, whose bytes do not match the declared type,
  or that exceeds 1 MB is rejected before it can be embedded in a report.

Placement overrides (`test_asset_overrides.py`):

- an override set for environment and business service survives the next
  `upsert_asset`, while the source columns are still updated by that sync (the
  requirement that the AssetExplorer integration keeps working);
- clearing the override returns the source value with no re-sync;
- a PUT with both fields blank clears the override rather than storing blanks;
- the override is attributed to the operator and audited with the old and new
  values and the reason;
- the asset's AssetExplorer id, hostname, IP and reconciliation state are
  unchanged by an override.

The placement vocabulary (`test_placement_options.py`):

- an option is stored trimmed and appears in its kind's list;
- the same value cannot be listed twice, including a different case or with
  surrounding whitespace (409, one row);
- a whitespace-only value is rejected (422) and nothing is stored;
- renaming an option changes the effective value shown for every server holding
  the old spelling, while `environment_source` keeps what AssetExplorer
  reported; the audit context records old and new values and that the rename
  propagated;
- a rename survives the next `upsert_asset` that still reports the old value —
  the source record is never rewritten, so the sync cannot undo it;
- an override saved under the old spelling resolves to the new name while the
  override row keeps the operator's original value;
- a former name cannot be re-added as a new option, and a second entry cannot
  be renamed onto another entry's former name (both 409);
- deleting a value still in use is refused (409) and the list is unchanged;
  deleting an unused value removes it and is audited.

Dashboard breakdowns (`test_dashboard_breakdown.py`):

- the OS panel groups RHEL assets under the `rhel` family rather than the
  version, and returns only the four family keys;
- an environment and business-service override moves the asset into the new
  group in `/summary` itself, not merely in the view;
- a Settings rename reaches the same aggregation: `/summary` returns the new
  environment and business-service names, which is what the panels draw;
- nine business services produce nine group rows — the API never truncates the
  list that the panel draws.

Integration configuration tests (`test_integration_test_endpoint.py`):

- a successful probe returns success, the detail and a duration, and writes an
  `integration.tested` event;
- a probe writes no `sync_runs` row, so testing cannot look like a collection;
- a connector failure is returned as `status: failed` with its reason instead
  of raising a 500;
- a draft endpoint is used for the test but is not persisted;
- a missing endpoint and a pasted credential are refused before any request.

Report file retention (`test_report_retention.py`):

- the default window is 2,555 days until an administrator stores a value;
- saving a window recomputes every completed report's `expires_at` from its
  completion date and runs the purge in the same request, so a shortened window
  deletes expired files immediately;
- a purged report keeps its row, status and digest, and its `file_path` becomes
  empty with the retention tombstone recorded;
- extending the window moves the expiry forward but cannot restore a file that
  was already deleted;
- a window outside 30–36,500 days is refused at the model.

HTTPS certificate upload (`test_tls_settings.py`):

- an enabled gateway with no material reports `serving: false` until the
  first start or upload;
- an administrator can install a pair; the response carries public metadata
  and never the private key, and the key file is mode 0600;
- the audit event records subject, issuer, expiry and fingerprint, not the key;
- a mismatched pair is refused with 422 and writes nothing;
- an upload is refused with 409 when the gateway does not terminate TLS;
- a second upload replaces the first;
- a non-administrator is refused by the real authorization check before any
  write.

SMTP relay settings (`test_smtp_settings.py`):

- the environment values are used, and reported as `source: environment`,
  until an administrator saves a relay;
- saving encrypts the password, stores a token that is not the plaintext, and
  no view returned by the API contains the password;
- a later save that omits the password keeps the saved one, while the first
  save carries over an unchanged environment username and password;
- `clear_password` removes authentication, and combining it with a new
  password is refused;
- the audit event records the host and a password-changed flag but never the
  value;
- decrypting with a rotated key yields nothing, so delivery reports a missing
  password rather than sending unauthenticated;
- the test route records `success`/`failure` (never the message or password),
  answers 502 on relay failure and 409 before sending when no relay is set.

Audit retention (`test_audit_retention.py`):

- the default window is 2,555 days until an administrator stores a value;
- the window round-trips through `shared.app_settings`, and changing it writes
  a `settings.updated` event with the new value;
- a window outside 30–36,500 days is refused at the model and again by the
  database function, so a row written by another path cannot widen the purge;
- a purge deletes only events older than the window, keeps newer ones and
  records `audit.purged` with the retention applied;
- `UPDATE` and an ordinary `DELETE` are still refused by the trigger outside
  the purge function.

Historical dashboard periods (`test_dashboard_periods.py`):

- a month view returns the snapshot captured inside the month — its id, its
  counts and its percentage — not a fresh evaluation of the current estate;
- the snapshot records the `os_family` panel, so the OS breakdown is available
  for a period rather than silently empty;
- quarter and year views resolve to the same recorded snapshot;
- `/snapshot-periods` lists month, quarter and year keys newest first with the
  number of daily snapshots behind each;
- an empty period is a 404, and an invalid key is a 400, never a blank success;
- a period combined with a dimension filter is refused with 400;
- an older snapshot without `os_family` rows returns an empty panel and lists
  the dimensions it does have, rather than filling the gap from live inventory;
- reading a period twice does not modify the stored snapshot.

## 4. Browser smoke test

`tests/ui/smoke.py` signs in and visits all 14 routes. It fails on:

- a console error or an unhandled promise rejection;
- the text "This view could not be loaded";
- an empty view element;
- fewer than 5 `.stat` tiles, or fewer than 4 chart panels on the dashboard.
  A `.chart-empty` placeholder counts as a panel: on a fresh installation
  every chart correctly shows its empty state, and demanding SVG axes there
  would fail a healthy deployment;
- a Timeframe control that does not start with the three Live options, still
  shows a separate Basis control, or does not swap in the matching trend panel
  or persist the choice across a reload;
- an auto-refresh control missing any of its four documented settings;
- a Timeframe control that does not list the recorded periods, or a selected
  period that fails to name its snapshot in the header, leaves the live server
  list or the dimension filters on screen, or does not return to the live
  panels when a Live timeframe is chosen;
- TV mode that does not hide the navigation through the button, `K`, `Esc`
  and the `#/dashboard?tv=1` deep link, or that fails to restore it;
- any of the eleven registers missing a search box or a Sort by control;
- a populated register with no Export CSV button, an export whose row count does
  not match the active filter, a paged register whose export stops at the first
  page instead of covering the whole matching set, or an Export CSV button on
  the Executive dashboard, Operational health, Settings or Reports;
- a search that does not narrow the rows, a count line that does not report the
  filtered subset, clearing the search not restoring every row, or the order
  button not reversing the sort;
- a search that only matches the first column: on the server list a term that
  occurs only in the business-service column must match, and on remediation a
  term that occurs only in the source column must match;
- an inventory **Status** filter that does not select exactly one state, an
  inventory search for `Compliant` that returns any `Non-compliant` row, or a
  search box that loses focus, text or caret position across the debounced
  re-render;
- a placement override that is saved but not marked on the row, or that does
  not clear back to the AssetExplorer value;
- a Settings placement list that cannot add, rename or delete a value, an
  in-use value whose **Delete** is not disabled, or a placement editor that
  still offers free text after a list is configured.

```bash
PATCHTRACK_URL=https://127.0.0.1:8005 PATCHTRACK_USER=admin \
PATCHTRACK_PASSWORD=… python tests/ui/smoke.py --screenshot .runtime/dashboard.png
```

This is not part of the pytest suite: it needs a browser and a live stack. It
is the only check that the UI actually renders rather than merely returning
200. The browser context ignores certificate errors because the development
stack serves HTTPS with a self-signed certificate; set
`PATCHTRACK_TLS_ENABLED=false` and use an `http://` URL to run it against a
plain-HTTP stack instead. Its `--screenshot` option produces an artifact for visual review.

## 5. What the tests have already found

Twenty real defects, all fixed:

1. **Freshness slack.** The engine rounded assessment age to two decimals
   before comparing it with the threshold, so an assessment up to 30 seconds
   past the boundary still counted as fresh. Fixed by comparing the unrounded
   duration in `evaluate_asset`.
2. **Phantom IP changes.** `ip_address::text` yields `10.0.0.1/32`, so every
   unchanged re-sync looked like an IP change and raised a spurious
   `identifier_changed` conflict. Fixed by comparing `host(ip_address)`.
3. **Month-end scheduler drift.** A schedule on the 26th whose run time had
   passed advanced to the 28th; a 31st schedule could not run in February.
   Fixed with `calendar.monthrange` clamping per target month.
4. **Sign out silently failed.** The sign-out button posted to
   `/api/session/logout` without the CSRF header, so the gateway answered 403
   and the cookie survived while the UI pretended otherwise.
5. **A crafted `X-Forwarded-For` returned 500 on sign-in.** The services read
   the raw header and passed it to `inet` columns; unparseable text aborted the
   request. Addresses now come from `request.client` (uvicorn honours the
   header only for trusted peers), and an unparseable value is stored as NULL.
6. **Forced password rotation was never enforced.** `must_change_password` was
   written on user creation and on an administrator reset but read nowhere,
   and there was no self-service change endpoint at all — the flag could only
   be cleared by a second administrator reset. Now carried in the access token,
   enforced by a gateway 403 on every proxied route, satisfiable through
   `POST /auth/change-password`, and prompted for in the UI.

The container defects below were all found by the first full compose run
(section 7); none of them was caught by the code-level suite, which is why the
delivery path now has its own tests.

7. **The image would not build.** UBI9's python image sets `USER 1001`, so the
   ownership step ran unprivileged and failed.
8. **The default database image did not exist.** Red Hat publishes the
   PostgreSQL image behind a subscription; the default is now the official
   `postgres:17` (was `postgres:13`, which reached end of life).
9. **The `migrate` container never ran.** The entrypoint ignored compose's
   `CMD`, demanded `PATCHTRACK_SERVICE` and exited 2, blocking every service
   that waits on it.
10. **`PATCHTRACK_LOG_LEVEL=INFO` crash-looped.** uvicorn accepts lowercase
    level names only; the entrypoint now normalises the case.
11. **`email-validator` was missing from the image**, so identity exited on
    import. A test now resolves every service import to a declared dependency.
12. **The image applied no migrations and the gateway proxied to itself.** The
    runner looked for the SQL at a fixed depth that is wrong in the image, and
    the backend URLs defaulted to the gateway's own loopback. The deployment
    reported success with an empty schema. Discovery now searches upward and a
    missing directory is an error; compose sets the backend service names.
13. **The job runner only understood Docker.** On RHEL and Rocky, where Podman
    is the shipped runtime, it fell back to the native transport and then
    failed to reach a port the container deployment does not publish. It now
    detects `docker compose`, `podman-compose` or `podman exec`, and the
    container path was run against the live stack.
14. **The server-list search box returned everything.** The console sent the
    term as `q`; the endpoint reads `search`, so the request succeeded and the
    filter was ignored — a search that looks successful but shows the whole
    estate. The parameter is fixed, and the browser test now asserts that a
    search narrows the rows, the count line says so, and clearing it restores
    them.
15. **Searching a state name returned both states.** The SQL matched compliance
    state with `ILIKE '%term%'`, so `Compliant` also matched `Non-compliant` and
    a status search showed both. States are now matched as whole terms, and
    Server inventory has an explicit Status filter for an exact state.
16. **The search box lost focus and caret at every pause.** The debounced
    server-side search replaced the input mid-word; the caret vanished and the
    operator had to click back in. The box now restores its text, focus and
    caret after the re-render, superseded responses are discarded, and the
    debounce is 500 ms. The browser test types into the box and asserts where
    focus and the caret are afterwards.
17. **The business-service panel hid every group after the seventh.** The
    row chart sliced the API response to seven rows with no notice, so an
    eighth service — including a newly configured or locally corrected one —
    simply was not on the dashboard. The panel now draws every group and
    scrolls when the list is long. The environment panel was checked at the
    same time and does move a server between groups when its environment
    override changes (`test_dashboard_breakdown.py`).
18. **The operating-system panel grouped by version.** It showed `9.4`,
    `2022`, `11.4` instead of the platform, so the three supported platforms
    could not be compared. `/summary` now groups by `os_family`, and the panel
    labels the families RHEL, Microsoft Windows, Solaris and Other.
19. **A Settings rename never reached the inventory or the dashboard.** The
    list changed, but every server kept the old spelling, so the inventory,
    filters, panels, engine, reports and Grafana disagreed with Settings — and
    the next sync would have restored the old value anyway had it been written
    through. Renames now record the old spelling in
    `inventory.placement_aliases` and `inventory.asset_effective` resolves it on
    read: every reader shows the new name, the source value survives as
    `*_source`, and re-syncs cannot undo it. `test_placement_options.py` and
    `test_dashboard_breakdown.py` pin the propagation, including through the
    `/summary` aggregation the panels consume.
20. **A value still in use could be deleted from Settings.** Deletion was
    allowed unconditionally, so the list could stop recognising a placement
    the inventory still displayed. Deletion is now refused (409) while any
    server holds the value; rename is the way to change it. The Settings button
    is disabled in that case, and the browser test asserts it.

## 6. Coverage gaps, stated honestly

| Area | Status |
|------|--------|
| Connector HTTP behaviour | **Not tested against live systems.** Unit tests do not mock the source APIs end to end; the connectors are exercised only through normalisation logic and the sync path with injected clients. |
| Real AssetExplorer / Satellite / SCCM field names | **Unverified.** The mapping is configuration-driven precisely because the installation's field names are unknown. See the verification checklist in each integration document. |
| Email delivery | **Not tested against a real SMTP server.** `send_report_email` returns `skipped` when unconfigured; the failure path is exercised only by inspection. |
| PDF visual layout | **Not asserted.** Tests check validity, size, EOF and digest, not whether a table overflows. Review a generated report visually after any change to `pdf.py`. |
| Grafana provisioning | **Verified by procedure (§7), not automated.** |
| Docker Engine topology | **Executed under Podman 5.8.2, not Docker Engine.** The same compose file brought the stack up end to end; Docker's own label and health-check handling should be confirmed on the target. |
| SELinux enforcing | **Exercised with Podman on an enforcing host** (`:Z` mounts worked without `setenforce`), not with the Docker daemon's relabelling. |
| Performance at scale | **Not measured.** Estimates are in [architecture.md](architecture.md) and each integration document. |

These gaps are the reason [install.md](install.md) includes a verification
step and each integration document ends with a checklist.

## 7. Dashboard verification procedure

Run after any change to `scripts/gen-grafana-dashboard.py` or the datasource.

1. Regenerate:
   ```bash
   /opt/patchtrack-venv/bin/python -I scripts/gen-grafana-dashboard.py
   ```
   (Run with `-I` so the generated JSON is read with no local imports on the
   path.)
2. Validate every query against PostgreSQL in both the "All" and a filtered
   form: 15 distinct queries × 2 variants = 30 variants. Each must `PREPARE`
   without error.
3. Execute every panel through Grafana's own `/api/ds/query` and confirm
   `status: OK`.
4. Render the dashboard in a browser and check for label collisions and
   categorical axes that got reformatted as numbers.
5. Compare three figures — in-scope count, compliant count, percentage —
   against `/api/compliance/summary`. They must be identical.

A quick manual spot check of agreement:

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8003/summary | jq .kpis
psql -c "SELECT compliant_servers, total_servers, compliance_pct
         FROM compliance.snapshots ORDER BY captured_at DESC LIMIT 1"
```

## 8. Deployment verification on RHEL 9.8 / Rocky 9.8

| Step | Pass condition |
|------|----------------|
| `docker compose build` | Image builds from UBI9 python-312 |
| `docker compose up -d` | postgres healthy, migrate exits 0, five services healthy |
| `/readyz` on each port | 200 |
| Sign in | Succeeds; bootstrap form gone |
| `docker compose stop grafana` | Application unaffected |
| Generate a report | `completed`, download digest matches |
| `ausearch -m avc -ts recent` | No denials for the containers |
| Reboot the host, `docker compose up -d` | Everything returns healthy; snapshots and schedules intact |
| `sha256sum` of a report before and after restart | Unchanged |

## 9. Adding a test

- A change to a published compliance figure requires a change to
  `test_engine.py`. If it does not, the change is either untested or
  inconsequential.
- A change to SQL that touches reconciliation needs an integration test; the
  `/32` bug is the precedent.
- A change to a route's permission needs a case in `test_security.py`'s role
  matrix.
- Prefer asserting the *reason* text as well as the state for engine tests: the
  reason is what an auditor reads.

## 10. Continuous integration

A suitable CI job:

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r deploy/container/requirements-dev.txt
python -m pytest -q                    # unit; integration skips without a DB
```

and a second job with a PostgreSQL service container and
`PATCHTRACK_TEST_DATABASE_URL` set, running the full 200.

Do not run the browser smoke test in CI unless a Chromium is preinstalled and
the stack is started; it belongs to a deployment pipeline stage, not a unit
stage.
