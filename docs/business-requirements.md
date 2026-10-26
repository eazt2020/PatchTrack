# Business requirements

Status: baseline, recorded 2026-10-26. Requirement identifiers (`BR-n`) are
used throughout the documentation set for traceability; see
[traceability-matrix.md](traceability-matrix.md) for the implementation
status of each.

## 1. Purpose

The organisation must be able to state, at any point and for any past period,
how much of its server estate carries the patches it is required to carry by
policy — and be able to defend that number in an audit.

Today that question is answered by counting rows in several different
management consoles, with different populations, different definitions of
"compliant", and no record of how the number was produced. The result is not
reproducible and not defensible.

PatchTrack exists to produce one number, from one documented population, by
one documented rule, frozen in an immutable record at the moment it is
reported.

## 2. What PatchTrack is, and is not

PatchTrack **is**:

- a read-only aggregator of inventory and patch-assessment data;
- a deterministic compliance evaluator with an explicit, versioned policy;
- a reporting system that produces archival PDFs and a live dashboard over
  the same underlying figures;
- an auditable record of who viewed, generated, changed or excluded what.

PatchTrack **is not**:

- a patch deployment tool. It does not install, remove, schedule or approve
  patches;
- a configuration management database;
- a vulnerability scanner;
- an authority that declares the organisation compliant with any regulation.

## 3. Business objectives

| ID | Objective | Measure |
|----|-----------|---------|
| BO-1 | One compliance number the organisation trusts | Dashboard, API and PDF report for the same period and policy report identical figures |
| BO-2 | Defensible in audit | Every reported figure is reproducible from a stored snapshot plus the policy version in force at capture |
| BO-3 | No false comfort | Servers with missing or stale data never count as compliant |
| BO-4 | Reduce manual reporting effort | Monthly and quarterly reports generated and delivered on a schedule |
| BO-5 | Segmented accountability | Compliance visible by OS, environment, business and application group |
| BO-6 | Operational transparency | Every collection, exclusion, policy change and report is attributable and time-stamped |
| BO-7 | Deployable in the existing estate | Runs on RHEL 9 and Rocky 9 with SELinux enforcing, no direct SSH to managed hosts |

## 4. Functional business requirements

### BR-1 Authoritative inventory

The server population is taken from ManagedEngine AssetExplorer. AssetExplorer
is the authority for asset identity, hostname, OS, environment, business group
and lifecycle status. PatchTrack does not invent assets, and does not delete an
asset because it briefly disappeared from a collection response.

### BR-2 Authoritative patch assessment

Patch status comes from the system that actually manages patches on that
platform: Red Hat Satellite for RHEL, Microsoft Configuration Manager (SCCM)
for Windows, and an approved read-only collection process for Solaris. Where a
source reports an attribute, PatchTrack uses it; where a source does not
provide an attribute, PatchTrack records it as unknown rather than
substituting a plausible value.

### BR-3 Five compliance states

Every in-scope server carries exactly one of: Compliant, Non-compliant,
Unknown, Stale, Excluded. There is no sixth "probably fine" state.

### BR-4 Documented numerator and denominator

The compliance percentage is server-level: compliant servers divided by
compliant + non-compliant + unknown + stale. Unknown and stale are counted
against compliance, never trimmed from the denominator. Patch counts are
reported separately and are never blended into the server percentage.

### BR-5 Freshness

An assessment older than the policy's freshness threshold is Stale, not
Compliant. The threshold is configurable and recorded with every snapshot.

### BR-6 Scope and exclusion

Servers may be excluded from the denominator only by an explicit, recorded
decision: out of scope in AssetExplorer, retired/decommissioned lifecycle, or
an excluded environment named in policy. Every exclusion carries a reason and
an actor.

### BR-7 Immutable historical record

A daily compliance snapshot is captured automatically. Snapshots are never
updated or deleted. A past period reported in March must still report the same
figures in December.

### BR-8 Reporting

Monthly management and quarterly compliance reports are generated as
professional PDFs with an executive summary, breakdowns, remediation overview,
trend, methodology and data-quality statement. Each archived report is
identified by the SHA-256 of its bytes. A report is never silently replaced by
a different result for the same period.

### BR-9 Delivery

Reports can be emailed to a configured recipient list. Delivery failure is
recorded separately and never invalidates a successfully generated report.

### BR-10 Dashboard

A Grafana dashboard presents the current position and historical trend, using
the same definitions as the API and the reports. The core application must
remain fully functional when Grafana is unavailable.

### BR-11 Roles

Four roles — Viewer, Operations, Report Manager, Administrator — are enforced
by the backend on every request. The UI hides controls a role cannot use, but
the UI is never the control.

### BR-12 Audit

Authentication, authorisation failures, collection runs, exclusions, policy
changes, report generation and delivery, integration changes and setting
changes are recorded append-only with actor, role, time, outcome and source
address.

### BR-13 Integrations are explicit

No integration is enabled by default. Each requires an endpoint and a
credential reference configured by an administrator. A failing integration is
surfaced as degraded, never as "zero findings".

### BR-14 Portability

The application installs and runs on Red Hat Enterprise Linux 9.8 and Rocky
Linux 9.8 without modification, with SELinux enforcing, using container
images and packages available for both.

## 5. Constraints (non-negotiable)

These are delivery constraints, not preferences. Each has a corresponding
verification in [testing-strategy.md](testing-strategy.md) or a control in
[security.md](security.md).

| ID | Constraint |
|----|------------|
| C-1 | Do not deploy universal default credentials. |
| C-2 | Do not record passwords, access tokens, private keys, or unnecessarily sensitive payloads. |
| C-3 | Do not commit production credentials or private keys to source control. |
| C-4 | Do not disable TLS verification to work around connectivity issues. |
| C-5 | Do not require SELinux to be disabled to run the application. |
| C-6 | Do not assume that direct SSH collection is permitted. |
| C-7 | Do not silently associate a patch assessment with the wrong asset. |
| C-8 | Do not silently replace an archived report with a different result. |
| C-9 | Do not substitute a static interface or fabricated production data for functional business capabilities. |
| C-10 | A failed email must not invalidate a successfully generated report. |
| C-11 | If a source system does not provide a particular attribute, PatchTrack must not fabricate it. |
| C-12 | The system must not treat unknown or stale records as compliant. |

## 6. Out of scope for this release

- Patch deployment or remediation automation.
- Agent installation on managed servers.
- Vulnerability scanning or CVSS scoring beyond what the source systems report.
- Non-server asset classes (network devices, endpoints, cloud PaaS).
- Multi-tenancy.
- Enterprise IdP sign-in. The schema and role model accommodate it
  (`identity.users.auth_source`), but no OIDC provider is configured.

## 7. Success criteria

The release is accepted when:

1. A fresh installation on Rocky 9.8 with SELinux enforcing reaches a working
   dashboard, API and PDF report without disabling SELinux or any TLS check.
2. For a given period and policy version, `/api/compliance/summary`, the
   Grafana dashboard and the generated PDF agree on every published figure.
3. A server with no assessment is reported Unknown, and the compliance
   percentage including it is lower than the percentage excluding it.
4. Stopping Grafana has no effect on authentication, the API, report
   generation or scheduled delivery.
5. Every write operation in the audit trail can be attributed to a named user
   or to `system`.
