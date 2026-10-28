# User guide

Audience: anyone who signs in to PatchTrack to read compliance position,
investigate the servers behind it, or produce a report.

## 1. Signing in

Browse to the PatchTrack address your administrator gave you. Enter your
username and password.

- Five wrong passwords lock the account for 15 minutes. Wait, or ask an
  administrator to reset it.
- On your first sign-in, or after an administrator reset, you must choose a new
  password. Use at least 12 characters; a passphrase is better than a short
  complex string.
- The session lasts 30 minutes of use. After that you are returned to the login
  page; there is no silent background refresh in the browser.

If the page offers **Create administrator**, the system has never been
initialised. That form appears only when no user exists.

## 2. The dashboard

The first screen is the executive view for the whole estate, filtered by any
selection in the filter bar.

**Five tiles across the top:**

| Tile | Meaning |
|------|---------|
| Total servers (in scope) | The population being judged; the sub-line counts the servers excluded from scope |
| Compliant | No outstanding blocking findings, with a fresh assessment |
| Non-compliant | One or more outstanding blocking findings |
| Unknown / stale | No usable assessment, or one older than the freshness threshold |
| Compliance vs target | The percentage and the gap to the policy target |

**Compliance percentage** is:

```
compliant ÷ (compliant + non-compliant + unknown + stale)
```

Unknown and stale count **against** compliance. They are not quietly removed
from the bottom of the fraction. If the number looks worse than another tool's
figure, that is usually why — the other tool is excluding data it does not
have.

If the denominator is zero the tile shows `n/a`. An empty estate is not a
perfect one.

**Charts:**

- **Compliance by operating system / environment / business service** —
  stacked bars showing the composition of each group. Operating system is the
  platform family (RHEL, Microsoft Windows, Solaris, Other), not the version.
  Environment and business service are the **effective** values, so a local
  placement correction moves the server into its new group on the next render.
  Every group is listed; a long list scrolls rather than hiding the tail.
- **Patch status distribution** — donut of the four states.
- **Compliance trend** — the recorded percentage over the granularity selected
  in the Timeframe control (monthly, quarterly or yearly), from immutable daily
  snapshots, with the target line. Monthly is drawn as a line; quarterly and
  yearly as bars, one bar per period.
- **Period summary** — the same observations as a table: compliance, target,
  the change against the previous period, the average server count, and the
  number of daily snapshots the period rests on.

Patch counts by severity are on **Compliance → Remediation**. They are separate
from the server percentage and never combined with it, so the executive view
keeps the two figures apart rather than blending them into one score.

**Timeframe, auto-refresh and wall displays.** The controls to the right of the
filter bar choose which point in time the tiles describe, how the trend is
grouped, and how often the page reloads itself:

| Control | Values | Notes |
|---------|--------|-------|
| Timeframe | Live · monthly trend (12 points), Live · quarterly trend (8), Live · yearly trend (5), then every month, quarter and year with a recorded snapshot in its own group; the picker reaches back five years (60 months, 20 quarters, 5 years) | The single control answers both questions. A Live choice shows today's estate and groups the trend by the granularity named in the option. A recorded month, quarter or year shows the tiles and breakdowns as they were recorded in that period — the latest daily snapshot captured inside it — and groups the trend by that period's own kind (month → monthly, quarter → quarterly, year → yearly). |
| Auto-refresh | Off, 30 seconds, 1 minute, 5 minutes | Reloads the tiles and charts in place. It pauses while the browser tab is hidden and while a dialog is open, and resumes on the next tick. |
| TV mode | on / off | Hides the left navigation and the header so the dashboard fills the screen. Move the pointer to the very top edge (or press Tab) to reveal the header again. |

**Historical periods.** Choosing a month, quarter or year replaces the
freshness line with the provenance of the snapshot it is showing — for example
"Period: October 2026 · snapshot captured 27 Oct 2026 06:00 · 27 daily snapshots
in the period". The tiles, composition panels, donut and severity bars are the
recorded figures, not a recalculation of today's estate, so a past month stays
the same on every visit. The line above the tiles always names the period and
the number of snapshots behind it.

Two parts of the screen change in a historical view:

- **Filters are hidden.** A snapshot stores group totals, not server rows, so
  an environment or business-service filter cannot be applied to it honestly.
  The API refuses such a combination; choose a **Live** timeframe to filter.
- **Recent non-compliant servers is replaced** by a note explaining the
  snapshot, because that list is live and would contradict the frozen tiles
  above it. The business-service table remains, drawn from the snapshot.

A period appears in the list only after a daily snapshot exists inside it. The
trend and period-summary table under the tiles are not limited to the selected
period: they show the whole window for that granularity (12 months, 8 quarters
or 5 years), so the selected period can be read in context. Choosing a recorded
period also sets that granularity — Q3 2026 shows the quarterly trend and
summary. Their figure for a period is the **average of every daily snapshot**
in it, while the tiles show the latest one, so the two can differ; the note
beside the tiles states which snapshot they came from.

The choices are remembered per browser, so a wall display keeps them after a
reload. TV mode is presentation only: it does not change what your role may
see.

- Press **K** to toggle TV mode and **Esc** to leave it.
- In TV mode the header (title, freshness line, refresh interval and buttons)
  slides off screen. Hover the top edge of the window to bring it back; it
  hides again when the pointer leaves. Keyboard focus reveals it too, so the
  controls remain reachable without a mouse.
- `#/dashboard?tv=1` opens the dashboard already in TV mode for a bookmarked
  NOC/SOC display; the parameter applies to that visit and does not overwrite
  the stored preference.
- Auto-refresh keeps the page signed in only as long as the session lasts. When
  the session expires the next refresh returns you to the sign-in page instead
  of leaving stale numbers on screen.

**Freshness line** under the title shows when inventory and patch data were last
collected. If it is older than you expect, the number below it describes the
past, not the present.

## 3. Filters

The filter bar applies to the dashboard, the server list and the findings view.
It is a live-estate control: when the dashboard's **Period** is a past month,
quarter or year, the filter fields are hidden and cleared, because a snapshot
holds group totals rather than server rows.

| Filter | Values come from |
|--------|------------------|
| Environment | AssetExplorer |
| Business service | AssetExplorer |
| Operating system | AssetExplorer |
| Application group | AssetExplorer |
| Patch source | Which system assessed the asset |

Filters are ANDed. Clicking a chart segment or a bar applies the matching
filter — the fastest way to answer "which environment is dragging the number
down".

The filter selection is not saved between sessions; the URL hash carries it, so
a bookmark reproduces a view.

### Narrowing and sorting any list

Every register — non-compliant servers, remediation, the snapshot ledger,
server inventory, reconciliation, report history, schedules, integrations,
compliance policy, users and the audit trail — has the same three controls
above the table:

| Control | What it does |
|---------|--------------|
| Search | Matches **any column of the row**, not one nominated field: a single term can hit a hostname, an operating system, an environment, a business service, a status, an actor, an advisory title, a source, an owner, a location or a note. Matching is case-insensitive and updates as you type. |
| Field filters | One dropdown per useful field (severity, status, source, role, cadence…). They combine with each other and with the page filter bar. |
| Sort by + Order | Choose the field, then press the order button to switch between ascending and descending. |

- The line under the table says **"Showing X of Y rows"** whenever a filter or
  search is active, so a narrowed list can never look like missing data.
- Rows without a value (a server never assessed, a schedule never run) sort
  last in both directions: "never" is not "oldest".
- On **Non-compliant servers** and **Server inventory** the list is paged by the
  service, so sorting is applied to the whole matching set, not just the 25 rows
  on screen. The chosen sort survives paging and searching.
- Filter and sort choices are kept while you stay in the console; reloading the
  page keeps them for that view until you sign out.
- **A status name is matched as a whole term.** Searching `Compliant` returns
  compliant servers only; it never also returns `Non-compliant` rows. To pick an
  exact state, use the **Status** filter on Server inventory or the state tabs
  on Non-compliant servers — a filter is always the unambiguous way to ask.
- The search box keeps the **text and caret** while the paged lists re-fetch, so
  you can keep typing without clicking back into it after each pause.
- Register tables have an **Export CSV** button above them. It downloads the
  list as currently filtered and sorted — not merely the rows visible on screen:
  the paged registers fetch every matching page before writing the file. The
  file is named after the screen and today's date, uses the same visible values
  as the table (formatted dates, state labels), omits action-only columns, and
  is UTF-8 with a BOM so Excel opens it cleanly. The Executive dashboard,
  Operational health, Settings and Reports do not offer the button: those
  tables are presentation or operational views rather than registers.

## 4. Non-compliant servers

**Compliance → Non-compliant servers** lists the servers behind the tiles, with
tabs for Non-compliant, Compliant, Unknown and Stale.

Columns: hostname, IP, operating system, environment, business service, status,
critical finding count, total patches, last assessment date.

- Click a **hostname** to open the server detail: identifiers, ownership,
  compliance reason, outstanding findings, and its assessment history. From
  there, Operations and Administrators can correct the environment or business
  service locally — see [Editing environment and business service](#editing-environment-and-business-service).
- Search covers every column on the list — hostname, FQDN, IP, operating system, environment, business service and the compliance reason — not just the hostname. A state name matches as a whole term, so `Compliant` does not also return `Non-compliant` rows; use the tabs for an exact state.
- The status tab and the filters combine, so "Unknown in Production" is two
  clicks.

The **reason** column on the detail page explains the state in a sentence, for
example "Assessment is 51.2h old; freshness threshold is 48h". That sentence is
the one to quote in an audit.

### Verifying and justifying a failing server

A server that is non-compliant, stale or unknown needs a person to verify it.
Open its detail and use **Verification and justification**:

- Choose a **reason**: accepted risk, compensating control, remediation
  scheduled, false positive, decommissioning, collection gap or under
  investigation.
- State **what you verified** (at least 20 characters) and, where relevant,
  the remediation plan and target date.
- Attach **evidence** — up to five files of 5 MB each; PDF, PNG/JPEG, text,
  CSV, Word or Excel.

Recording a verification does **not** change the compliance state or the
numbers; it explains them and keeps the evidence beside them. Recording a new
one supersedes the old record but keeps it in the history with its evidence.
A justification can be withdrawn; history is retained either way. Operations
and Administrators can record or withdraw; anyone who can open the server can
read and download the evidence.

Every report produced while a server is failing lists it in the **Verified
exceptions and remediation** section. Servers with a recorded verification
show the statement, remediation plan, target date, recorder and evidence
filenames inside the table; servers without one appear in a second table with
their state, compliance reason and outstanding patch count. This records the
explanation alongside the figures it explains; it does not alter them.

## 5. Remediation

**Compliance → Remediation** lists individual outstanding patch findings with
hostname, advisory id, title, severity, source system, business service and
release date.

- Severity bars at the top summarise the current findings.
- Remediation deadlines come from the active policy: critical 7 days,
  important 30 days, moderate 90 days, low 180 days from publication.
- PatchTrack does **not** deploy patches. Take the advisory id and hostname to
  Satellite, SCCM or the Solaris process.

## 6. History and trends

**Compliance → History & trends** shows the monthly trend over twelve months
and the list of captured snapshots.

A snapshot is a frozen record: the population, the counts, the policy version
and the methodology, captured at a moment in time. It never changes. Monthly
reports are generated from snapshots, which is why a report for a past month
still shows the past month's number.

If the trend is empty, no daily snapshots have been captured yet — they
accumulate from the day the platform went into service.

## 7. Inventory

**Inventory → Server inventory** is the full asset list from AssetExplorer,
including assets outside the compliance scope. Use it to confirm what
PatchTrack believes about a server: hostname, FQDN, IP, OS, environment,
business group, application group, lifecycle, scope and patch source.

- **Exclude** takes a server out of the compliance denominator. It requires a
  reason and is recorded in the audit trail. Use it only for a server that is
  genuinely outside scope; a decommissioned server should be retired in
  AssetExplorer instead.
- **Include** reverses an exclusion.
- The **Search** box matches every column, including the ones not shown by
  default in a narrow window: owner, department, location, lifecycle, patch
  source and compliance state. A status name is an exact term — `Compliant`
  shows compliant servers only.
- The **Status** dropdown above the table selects one compliance state exactly
  (Compliant, Non-compliant, Unknown, Stale) and combines with the search box
  and the paging.

If a field is wrong, fix it in AssetExplorer. PatchTrack does not edit its own
inventory, by design — one authoritative record, one place to correct it.

### Editing environment and business service

Environment and business service are the exception, because a reorganisation
or migration is often recorded late in the source system. Operations and
administrators can correct either locally without touching AssetExplorer:

1. In **Server inventory**, press **Edit** in the **Placement** column (or open
   a server and press **Edit environment / business service**).
2. Choose the environment and/or business service from the list. Leave a field
   at **Use AssetExplorer value** to keep the source value for it.
3. Write a short reason — it is required and is recorded in the audit trail.
4. Press **Save override**.

The values in those lists are maintained by an administrator in
**Settings → Inventory placement**, not typed per server. That is what
keeps `Production` from being entered three ways in a week. If a needed value is
missing, ask an administrator to add it; until the list for a field is
configured, the editor falls back to free text.

The correction is stored *beside* the source value:

- The AssetExplorer sync **keeps running exactly as before**. It continues to
  update its own columns on every collection and never overwrites or clears the
  override. The asset stays matched by its immutable AssetExplorer ID, so
  reconciliation is unaffected.
- Lists, filters, the dashboard's environment and business-service panels,
  the compliance engine, reports and the Grafana dashboard all use the
  effective placement, so a correction moves the server into its new group on
  the next refresh. Overridden cells carry a **LOCAL** marker whose
  tooltip names the AssetExplorer value, and the server detail shows both.
  A value that an administrator renamed in Settings shows the new name with
  `(AssetExplorer: old name)` and no **LOCAL** marker — it is the list, not a
  local correction, that changed it.
- If the environment changes which policy scope rule applies, saving the
  override re-evaluates compliance, so the persisted states follow immediately.
- **Use AssetExplorer values** in the same dialog removes the override; the
  source value applies at once, with no sync needed.
- Both actions are audited (`inventory.placement_overridden` and
  `inventory.placement_restored`) with the old value, the new value, the reason
  and the operator.

Compliance history is not rewritten. Snapshots already captured keep the
placement they were captured with; the next snapshot uses the effective value.

## 8. Reconciliation

**Inventory → Reconciliation** shows patch records that could not be matched to
an AssetExplorer server.

| Reason | What it means | What to do |
|--------|---------------|------------|
| `assessment_unmatched` | The patch system reports a host AssetExplorer does not have | Add it to AssetExplorer |
| `ambiguous_match` | Two assets share a hostname, FQDN or IP | Fix the duplicate in AssetExplorer, then resolve |
| `identifier_changed` | A patch system id now points at a different host than before | Confirm with the patch team, then resolve |

Resolving a conflict requires choosing the asset (or confirming there is none)
and writing a note. Nothing is guessed automatically — a patch assessment is
never attached to the wrong server.

An unattended conflict means those servers show as Unknown. That is the honest
answer, and the conflict list is where to fix it.

## 9. Reports

**Reporting → Reports** lists generated reports: type, period, status, size,
requested by and digest.

- **Generate** asks for a type and a month or quarter. A snapshot must already
  exist for that period; if not, the message tells you so and you can capture
  one from the same screen.
- **Download** retrieves the archived PDF.
- **Retry** applies to a failed report. It never replaces a completed one.
- **Delete** (administrators only) removes the selected archived reports and
  their PDFs permanently. Tick individual rows or use the header checkbox to
  select every row currently shown by the search and filters, then choose
  *Delete selected* and confirm. A report that is queued or still generating
  cannot be deleted. Deletion is audited per report and cannot be undone;
  retention remains the normal way for archived reports to expire.
- The **SHA-256** shown against a report lets an auditor verify that the copy
  they hold is the copy you issued.

Every report opens with a cover page carrying the organisation name and logo,
the report title, the reporting period, the preparer and the classification,
followed by a generated **Contents** page whose page numbers match the printed
report. The final page is an **Approval and sign-off** page with separate **Prepared
by** and **Concurred by** blocks — name, designation, signature line and date —
so the printed copy can be signed and retained with the archive. Configure the
names, designations and logo under **Administration → Settings → Report
branding**; the logo must be PNG or JPEG and no larger than 1 MB, and it is
stored with the settings so it survives a redeploy.

Report contents are described in [reporting.md](reporting.md). Every report
states its own methodology, population and denominator.

### Schedules

**Reporting → Schedules** creates recurring reports: monthly or quarterly, a
day of the month (1–28), an hour, a timezone and a recipient list. If no
timezone is chosen, the deployment timezone applies (see below).

- The report covers the period that **ended**, not the one in progress.
- A scheduled period runs at most once, even if two scheduler instances race.
- Email delivery is recorded separately. If the mail fails, the report is still
  generated and downloadable; ask for a resend rather than regenerating.

## 10. Administration

| Screen | Role required | Purpose |
|--------|---------------|---------|
| Integrations | Administrator | Endpoint, credential reference, TLS, enable, test configuration, run now |
| Compliance policy | Administrator | Target, freshness, blocking severities, scope rules |
| Users & roles | Administrator | Accounts, roles, password resets |
| Audit trail | Administrator | Who did what, when |
| Operational health | Operations | Integration status and manual collection |
| Settings | Administrator | Report branding (organisation name, logo, preparer and concurrence); environment and business-service lists; report file retention; audit trail retention |

### Testing an integration

**Administration → Integrations → Test configuration** checks the saved
configuration against the source without collecting anything. It proves the
endpoint is reachable, TLS verifies, the credential is accepted and the first
page has the expected shape. The result shows a short explanation and the
round-trip time.

- **Test configuration** on a card uses the saved values.
- **Configure → Test configuration** uses the values currently in the form,
  including an endpoint or credential reference you have not saved yet. A
  blank credential field tests with the saved reference.
- A test writes **no** assets, assessments or collection run, so it cannot
  change compliance figures or the integration's health. It is recorded in the
  audit trail as `integration.tested`.
- For Solaris there is no remote endpoint: the test checks that the inbox
  directory exists, is readable and every waiting document parses.

A failed test names the reason — missing endpoint, TLS error, HTTP status,
authentication failure, malformed payload or an unreadable inbox — so the
configuration can be corrected before a collection is attempted.

### Editing policy

1. Open **Compliance policy**, choose **New version**.
2. Change the target percentage, freshness hours, blocking severities or
   scope rules. Write a change note — it is shown in the report's methodology.
3. **Activate** the version. This re-evaluates the estate immediately.

Past snapshots keep the policy version they were captured with. Activating a
new policy does not rewrite history.

### Adding a user

**Users & roles → Add user**: username, display name, email, role, and an
initial password of at least 12 characters. Deliver the password out of band,
never by email with a report. The account must change it at first sign-in.

### Managing the placement lists

**Settings → Inventory placement** holds the environment and business
service values an operator can choose when correcting a server.

- **Add** a value: type it and press **Add** (or Enter). Duplicates are refused
  case-insensitively, so `Production`, `production` and `Production ` are one
  entry.
- **Edit** renames the value everywhere. Every server holding the old spelling
  shows the new name in the inventory, filters, the dashboard panels, the
  compliance engine, reports and Grafana on the next render. The *In use*
  column tells you how many servers a rename will carry. Compliance is
  re-evaluated after a rename, because a scope rule may match the environment
  by name.
- **Delete** is available only when *In use* is `—`. A value that servers still
  hold cannot be removed, because the inventory would then show a placement the
  list no longer recognises; rename it instead. Deleting an unused entry
  removes it from the picker.

A rename does **not** rewrite the inventory source. The value AssetExplorer
reports stays in `environment_source` / `business_group_source`, is shown beside
the effective value on the server detail, and the next collection cannot undo
the rename: the old spelling resolves to the new name on every read. Nothing is
written back to AssetExplorer, so correct the source there when you can — that
removes the need for the alias.

A former name cannot be added again as a new value while it still resolves to
its renamed entry, and renaming a second entry onto another entry's former name
is refused. Both would make one spelling mean two things.

The list starts as the distinct values already present in the inventory, and
the AssetExplorer sync never writes to it. If a list is emptied, the placement
editor falls back to free text rather than blocking the correction.

### Configuring report file retention

**Settings → Retention → Report file retention** sets how many days generated PDFs are
kept. The default is 2,555 days (about seven years); the accepted range is 30
to 36,500 days. The card shows how many reports are archived and how many
already await purge.

Saving applies the window immediately, with no restart: the expiry of every
completed report is recalculated from its completion date, and any file already
past the new window is deleted in the same request. Future reports record their
expiry when they complete, and the daily maintenance job continues to enforce
the window.

Only the PDF is deleted. The report record, its SHA-256, size and status remain
as an archive tombstone, so "this is the report we issued for October" is still
verifiable after the file is gone. Lengthening the window cannot restore a file
that was already purged. Every change is audited as `settings.updated`.

### Configuring audit trail retention

**Settings → Retention → Audit trail retention** sets how many days of audit events are
kept. The default is 2,555 days (about seven years); the accepted range is 30
to 36,500 days. The card shows how many events are stored and the date of the
oldest one.

The change is saved in the database and needs no restart. A daily maintenance
job deletes events older than the window; it never touches a newer event, and
the deletion itself is recorded in the trail as `audit.purged`. Apart from
that job the trail is append-only — there is no screen or API that edits or
deletes a single event. Every change to the window is audited as
`settings.updated`, so shortening retention cannot erase the record of who
shortened it.

Choose a window that matches your organisation's record-keeping obligation.
Shortening it permanently removes older evidence on the next run; lengthening
it cannot bring deleted events back.

## 11. Understanding the numbers

| Question | Where to look |
|----------|---------------|
| Why did compliance drop? | Compare the dashboard by environment/business service with last month's report |
| Why is a server Unknown? | Server detail → reason |
| Why is a server Stale? | Reason shows the assessment age and the threshold |
| Why is a server excluded? | Server detail, or the inventory row's exclusion reason |
| Why don't these numbers match another tool? | Check whether that tool excludes unknown/stale data, and whether the populations match |
| Is this report the one we issued? | Compare SHA-256 |
| When did we last collect? | Freshness line, and Operational health |

## 12. Keyboard and accessibility notes

- The UI is server-side rendered HTML with no build step; it is usable with a
  screen reader.
- Status is conveyed with a text label as well as colour.
- The page is designed for a desktop browser at 1280 px or wider. There is no
  mobile layout.

## System timezone

An administrator sets the deployment timezone in **Settings → Platform**. It
is the single zone PatchTrack uses to decide the local day of a daily
snapshot, the calendar boundaries of reporting periods, the default timezone
for new schedules, and the timestamps shown on screen and in generated PDFs.
Instants are still stored in UTC; changing the setting does not rewrite
existing snapshots or reports.

## 13. Getting help

Send your administrator: the screen, the period, the filter selection, and the
hostname or report id involved. Do not send passwords or `.env` contents.
