# Role model

Four roles, fixed in the schema (`identity.users.role CHECK`), enforced on the
backend by `require_permission`, and mirrored in the UI only to hide controls.

## Summary

| Capability | Viewer | Operations | Report Manager | Administrator |
|------------|:------:|:----------:|:--------------:|:-------------:|
| View dashboard, KPIs, trends | ✓ | ✓ | ✓ | ✓ |
| View server inventory and detail | ✓ | ✓ | — | ✓ |
| View non-compliant server list | ✓ | ✓ | ✓ | ✓ |
| View remediation / findings | — | ✓ | — | ✓ |
| View integration health | — | ✓ | — | ✓ |
| Run a manual collection | — | ✓ | — | ✓ |
| Exclude / include an asset | — | ✓¹ | — | ✓ |
| Override environment / business service | — | ✓¹ | — | ✓ |
| Resolve reconciliation conflicts | — | ✓¹ | — | ✓ |
| View, download reports | ✓ | ✓ | ✓ | ✓ |
| Generate a report on demand | — | — | ✓ | ✓ |
| Delete archived reports | — | — | — | ✓ |
| Capture a compliance snapshot | — | — | ✓ | ✓ |
| Manage schedules | — | — | ✓ | ✓ |
| Configure integrations | — | — | — | ✓ |
| Create / version / activate policy | — | — | — | ✓ |
| Manage users and roles | — | — | — | ✓ |
| View the audit trail | — | — | — | ✓ |
| Organisation settings, placement lists | — | — | — | ✓ |

¹ Asset exclusion and conflict resolution are `inventory:*` / `integrations:*`
capabilities held by Operations. They change the reported population, so they
are audited with a mandatory reason.

## Permission wiring

```python
PERMISSIONS = {
  "viewer": {"dashboard:view", "inventory:view", "inventory:detail",
             "compliance:view", "reports:view", "reports:download"},
  "operations": {..., "findings:view", "integrations:view", "sync:run"},
  "report_manager": {"dashboard:view", "inventory:view", "compliance:view",
                     "reports:view", "reports:generate", "reports:download",
                     "schedules:manage"},
  "administrator": {"*"},
}
```

`administrator` is not special-cased in route code; it holds the wildcard.

## Who should hold each role

| Role | Typical holder | Why |
|------|----------------|-----|
| Viewer | Executives, risk and audit, service owners reading dashboards | Read-only, no ability to alter the population |
| Operations | Platform / patch engineers, service desk leads | Runs collections, investigates gaps, manages exclusions — but cannot alter policy or reports |
| Report Manager | Compliance and governance staff | Owns the reporting calendar and the archived reports, without infrastructure access |
| Administrator | A small, named group | Integrations, policy, users. Should be 2–4 people with a documented handover |

Separation of duties that this model supports:

- The person who **produces** the report (Report Manager) is not necessarily
  the person who **configures the integrations** that feed it (Administrator).
- The person who **excludes** a server from scope (Operations) cannot silently
  change the target or the policy (Administrator).
- Everyone who reads reports (Viewer) cannot change anything.

## Account rules

- No shared accounts. The audit trail attributes every action to a username.
- No default or bootstrap password ships with the product. The first
  administrator is created through a one-time form that disappears once a user
  exists.
- Passwords are at least 12 characters and are hashed with Argon2id.
- Five failed attempts lock an account for 15 minutes. Unlock by waiting, or by
  an administrator resetting the password (which also clears the lock).
- An administrator cannot deactivate or demote the **last active
  administrator** — the API returns `409`.
- Role changes take effect at the user's next sign-in (tokens live 30 minutes).
  To force immediate effect, reset the password; that sets
  `must_change_password`.

## Creating users

Through the UI (Administration → Users & roles) or the API:

```http
POST /api/identity/users
{
  "username": "j.nguyen",
  "display_name": "Jordan Nguyen",
  "email": "j.nguyen@example.com",
  "role": "report_manager",
  "password": "<at least 12 characters, generated>"
}
```

The new user must change the password at first sign-in. Deliver the initial
password out of band; never email it with the report.

## Auditing access

`GET /api/identity/audit?actor=…&event_type=…&outcome=…` returns the trail,
filterable by time window. Everyone's sign-ins, including failures, are here.
Use it to answer "who excluded this server and when" and "who changed the
target".

## Known gaps

- No MFA and no OIDC sign-in in this release.
- No per-business-group scoping: a Viewer sees the whole estate. If business
  units must be isolated, deploy separate instances or add a scoping dimension
  to the role model.
- No session administration screen (list/revoke a user's sessions). Revocation
  today means a password reset.
