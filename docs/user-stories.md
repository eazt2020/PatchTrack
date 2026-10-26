# User stories and acceptance criteria

Each story carries acceptance criteria that can be checked against the running
system. None of them is satisfiable by a static screen or fabricated data.

## Personas

| Persona | Role | Cares about |
|---------|------|-------------|
| Priya, Head of Infrastructure | Viewer | Whether the estate is inside tolerance, and whether the number is true |
| Marcus, Patch Engineer | Operations | Why servers are Unknown or Stale, and which advisories are outstanding |
| Dana, Compliance Manager | Report Manager | Producing the monthly and quarterly pack, and defending it |
| Sam, Platform Administrator | Administrator | Onboarding sources, policy, users, and the audit trail |
| An external auditor | none (receives PDFs) | Reproducing a figure and proving a report was not altered |

## Compliance reading

### US-1 See the current position

> As Priya, I want one screen that tells me the compliance percentage and the
> composition of the population, so I can report it without assembling
> spreadsheets.

Acceptance criteria:

- [ ] The dashboard shows total in scope, compliant, non-compliant, unknown and
      stale counts and the percentage.
- [ ] The percentage equals `compliant / (compliant + non_compliant + unknown +
      stale)`, verifiable from the displayed counts.
- [ ] Unknown and stale are visibly part of the population, not a footnote.
- [ ] With an empty population the percentage reads `n/a`, not `0%` or `100%`.
- [ ] The freshness line shows when inventory and patch data were last synced.

### US-2 Explain a lower number

> As Priya, when my number is lower than another tool's, I want to understand
> why, so I am not accused of misreporting.

Acceptance criteria:

- [ ] Every report and the dashboard state the numerator and denominator.
- [ ] The unknown/stale contribution is separately visible.
- [ ] Drilling into a group shows the servers behind it.

### US-3 Track change over time

> As Priya, I want the trend over the last six months against the target.

Acceptance criteria:

- [ ] The trend is drawn from immutable daily snapshots.
- [ ] Fewer than two points produces an explanation, not a fabricated line.
- [ ] A past month's figure does not change when a later policy is activated.

## Investigation

### US-4 Find out why a server is not compliant

> As Marcus, I want the reason in plain language.

Acceptance criteria:

- [ ] The server detail shows the state and a reason sentence naming the cause
      (missing assessment, failed assessment, age and threshold, or blocking
      finding count).
- [ ] The outstanding findings are listed with advisory id and severity.
- [ ] The last assessment date is shown.

### US-5 See what needs patching

> As Marcus, I want the outstanding advisories, grouped and filterable.

Acceptance criteria:

- [ ] Findings list shows hostname, advisory, title, severity and source.
- [ ] Severity counts are shown.
- [ ] Remediation deadlines derive from the policy, not hard-coded guesses.
- [ ] Nothing in the UI claims PatchTrack deploys a patch.

### US-6 Resolve saved data that does not match

> As Marcus, I want to see patch records that did not match a server.

Acceptance criteria:

- [ ] Reconciliation lists each unresolved conflict with source, reference,
      match key and reason.
- [ ] An ambiguous match is never auto-assigned.
- [ ] Resolving requires a note and is audited.
- [ ] Until resolved, affected servers show as Unknown — not as compliant.

### US-7 Exclude a server from scope

> As Marcus, I want to exclude a genuinely out-of-scope server, with a reason.

Acceptance criteria:

- [ ] Exclusion requires a reason.
- [ ] The excluded server moves to the excluded count and out of the
      denominator.
- [ ] The exclusion appears in the audit trail with actor and time.
- [ ] Inclusion reverses it and is also audited.

## Reporting

### US-8 Produce the monthly pack

> As Dana, I want the monthly report generated from a frozen snapshot.

Acceptance criteria:

- [ ] Generation requires an existing snapshot; otherwise a clear error.
- [ ] The PDF contains KPI table, executive summary, breakdowns by OS,
      environment, business group and application group, remediation overview,
      trend, data-quality statement and methodology.
- [ ] The report says explicitly whether the target was met.
- [ ] The stored SHA-256 matches the downloaded file.

### US-9 Schedule it

> As Dana, I want the monthly report generated and emailed automatically.

Acceptance criteria:

- [ ] A schedule accepts cadence, day, hour, timezone and recipients.
- [ ] A past run time does not produce a report for the wrong period.
- [ ] Running the tick twice produces one report, not two.
- [ ] An SMTP failure leaves the report `completed` and records a failed
      delivery attempt.

### US-10 Defend a number from last quarter

> As an auditor, I want to reproduce a figure from a report I hold.

Acceptance criteria:

- [ ] The report states population, numerator, denominator, unknown treatment,
      freshness threshold, blocking severities and policy name.
- [ ] The snapshot row still holds those values and the counts.
- [ ] The snapshot has not been updated since capture (no update path exists).
- [ ] The stored digest verifies the file I hold.

## Administration

### US-11 Onboard a source system

> As Sam, I want to add Satellite without putting a password in the database.

Acceptance criteria:

- [ ] The integration form accepts a credential **reference** only; a pasted
      password is rejected with an explanation.
- [ ] The credential is resolved at collection time from the environment or a
      file.
- [ ] TLS verification is on by default.
- [ ] A test collection reports success with a plausible record count.
- [ ] The change is in the audit trail; the password is not.

### US-12 Change the policy

> As Sam, I want to tighten blocking severities for payment systems, without
> rewriting history.

Acceptance criteria:

- [ ] A new policy version is created with a change note.
- [ ] Activating it re-evaluates immediately and is audited.
- [ ] Prior snapshots keep their recorded `policy_version_id`.
- [ ] A management report for a past month still shows the past policy.

### US-13 Control access

> As Sam, I want the right people to have the right access.

Acceptance criteria:

- [ ] Four roles behave as documented in [roles.md](roles.md).
- [ ] A Viewer cannot generate a report or change an integration — enforced by
      the API, not just hidden in the UI.
- [ ] The last active administrator cannot be demoted or deactivated.
- [ ] Every sign-in, success or failure, is in the audit trail.

### US-14 Prove who did what

> As Sam, I want to answer "who excluded this server" and "who changed the
> target".

Acceptance criteria:

- [ ] Audit records carry actor, role, event type, target, outcome, time and
      source IP.
- [ ] Records cannot be edited or deleted through the application role.
- [ ] The audit API filters by actor, event type and outcome.

## Operations

### US-15 Know when collection has failed

> As Sam, I want to be told before the compliance number quietly degrades.

Acceptance criteria:

- [ ] Integration health distinguishes `authentication_failure`, `unavailable`,
      `degraded`, `stale` and `healthy`.
- [ ] A failed run is visible with an error summary containing no secrets.
- [ ] A failed run writes no assessment, so servers age into Stale rather than
      appearing compliant.
- [ ] The readiness endpoint fails when the database is unreachable.

### US-16 Install without breaking the platform's security posture

> As Sam, I want to deploy on Rocky 9 with SELinux enforcing and TLS on.

Acceptance criteria:

- [ ] The installation procedure contains no `setenforce 0` and no
      verification bypass.
- [ ] Containers run non-root with a read-only root filesystem and no
      capabilities.
- [ ] Backups and a restore are documented and tested.
- [ ] The stack can be installed with no internet access.

## Stories explicitly not delivered

| Story | Why |
|-------|-----|
| "Deploy these patches for me" | Out of scope: PatchTrack is read-only by design |
| "Sign in with our corporate identity provider" | OIDC not implemented in this release |
| "Show me only my business unit" | No per-scope role model; separate instances needed |
| "Scan servers for missing patches itself" | Patch state comes from the system that manages patches |
