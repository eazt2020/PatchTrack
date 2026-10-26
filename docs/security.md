# Security design

## 1. Principles

1. **No shipped credential.** No default password, key, token or service
   account exists in this repository, in a migration, or in an image.
2. **The backend is the control.** Every permission check happens in a service,
   on every request. The UI hides what a role cannot use; it is not a security
   boundary.
3. **Least privilege by construction.** One database role per function, a
   read-only role for the dashboard, non-root containers with a read-only root
   filesystem.
4. **Verify, do not disable.** TLS verification is on by default and turning it
   off is an explicit, audited act.
5. **Record, do not retain secrets.** Audit context is scrubbed; credentials
   are referenced, not stored.

## 2. Identity and authentication

| Control | Implementation |
|---------|----------------|
| Password hashing | Argon2id via `argon2-cffi`, `time_cost=3`, `memory_cost=65536` (64 MiB), `parallelism=4` |
| Password policy | Minimum 12 characters enforced by the request model; no maximum below 512 (the server never truncates silently) |
| Account lockout | 5 consecutive failures → 15-minute lock, stored on the user row |
| Enumeration resistance | Unknown user, wrong password and inactive account all return the same 401 body; the lockout check happens after the credential check |
| Access token | JWT HS256, 30 minutes, claims `sub`, `username`, `role`, `name`, `pwd_change_required`, `iat`, `exp`, `iss=patchtrack-identity`, `aud=patchtrack`; all required claims verified on decode |
| Refresh token | 48-byte URL-safe opaque value; only its SHA-256 is stored; revocable; 8-hour lifetime |
| Session cookie | `patchtrack_session`, `HttpOnly`, `SameSite=Strict`, `Secure` on any non-loopback host, path `/` |
| CSRF token | Second cookie `patchtrack_csrf` (readable by the SPA), compared with the `x-patchtrack-csrf` header in constant-ish equality on every state-changing API call |
| Origin check | Every state-changing `/api/*` request with an `Origin` header must match the request host exactly (scheme + host + port); suffix matching is not used |
| Forced rotation | `must_change_password` is set on creation and on administrator reset, carried in the access token as `pwd_change_required`; the gateway answers **403** on every proxied route until the user sets a new password. Satisfied by `POST /auth/change-password`, which requires the current password (a stolen cookie is not enough) and revokes all other refresh tokens |

### First-run bootstrap

`GET /api/session/status` asks identity whether zero users exist. If so the
login page shows a one-time form that creates an administrator. The identity
service refuses `POST /users` through this path once any user exists
(`409 Platform is already initialised`), and the bootstrap request itself is
only authorised by the internal service token. There is no bootstrap password
in `.env.example`, no seeded user, and no documented default.

## 3. Service-to-service trust

Backend services accept a principal from either:

1. a bearer JWT (verified signature, issuer, audience, expiry and required
   claims); or
2. trusted gateway headers (`x-patchtrack-user`, `x-patchtrack-role`,
   `x-patchtrack-user-id`, `x-patchtrack-internal`) **only** when
   `x-patchtrack-internal` equals `PATCHTRACK_SERVICE_TOKEN`, compared with
   `hmac.compare_digest`.

`PATCHTRACK_SERVICE_TOKEN` is empty by default. When empty, header-based trust
is disabled entirely, so a misconfigured deployment fails closed.

The role claim is validated against the known role list before use. An unknown
role is rejected with 403 rather than defaulted.

## 4. Authorization

`security.PERMISSIONS` maps role → permission set; `administrator` holds `*`.

| Permission | Used by |
|------------|---------|
| `dashboard:view` | summary, trends, breakdown |
| `inventory:view`, `inventory:detail` | asset listing and detail |
| `inventory:manage` | local environment / business-service overrides (attributed, audited) |
| `compliance:view` | server lists, snapshots, policy read |
| `findings:view` | remediation view |
| `integrations:view`, `integrations:manage` | integration health / configuration |
| `sync:run` | manual collection, daily job, scheduler tick |
| `reports:view`, `reports:download`, `reports:generate` | report listing, download, generation |
| `schedules:manage` | schedule CRUD |
| `settings:manage` | organisation branding and the environment / business-service picker (`/settings/*`) |
| `users:manage`, `audit:view`, `policies:manage` | administration |

Every route except `/healthz`, `/readyz` and the public session endpoints
depends on `require_permission(...)`. A missing check is a defect; the unit
test suite asserts the role matrix and that administrator-only capabilities are
withheld from the other roles.

See [roles.md](roles.md) for the human-facing description.

## 5. Transport

- All external traffic terminates TLS at a reverse proxy. The gateway sets HSTS
  at the proxy layer; the application emits `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
  `Permissions-Policy` and a strict `Content-Security-Policy`
  (`default-src 'self'`, no inline scripts, `frame-ancestors 'none'`).
- Uvicorn runs with `--proxy-headers` and `--forwarded-allow-ips` restricted to
  the proxy address (`PATCHTRACK_FORWARDED_IPS`, never `*`), so
  `X-Forwarded-For` cannot be spoofed by a direct client. No application code
  reads the raw header: services take the address from `request.client`,
  which uvicorn rewrites only for trusted peers, and a value that is not a
  valid address is recorded as NULL rather than failing the request.
- Database connections use `sslmode=require` or better. `verify-full` is the
  documented production setting; `disable` is never a supported value.
- Grafana to PostgreSQL uses `PATCHTRACK_GF_DB_SSLMODE` (default `verify-full`).

### Integration TLS

`inventory.integrations.verify_tls` defaults to true. The API rejects an
`http://` endpoint unless the caller explicitly sets `verify_tls=false` in the
same request, and that act writes a
`integration.tls_verification_disabled` audit event with the endpoint. There is
no global "insecure" switch.

The correct remedy for a certificate problem is to install the issuing CA on
the host — not to disable verification. See
[troubleshooting.md](troubleshooting.md#tls-verification-failures).

## 6. Secrets

| Secret | Where it lives |
|--------|----------------|
| Database password | `.env` / compose secret, mode 0600, loaded via `POSTGRES_PASSWORD` |
| `PATCHTRACK_JWT_SECRET` | 32+ bytes hex, generated at install |
| `PATCHTRACK_SERVICE_TOKEN` | 32+ bytes hex, generated at install |
| Integration credentials | Referenced by `secret_ref` (`env:NAME` or `file:/path`), resolved at collection time |
| SMTP password | Environment only |
| Grafana DB password | Set when creating the read-only role; supplied at role creation, never defaulted |
| Grafana admin password | `GF_ADMIN_PASSWORD` at install |

`secrets.validate_reference` rejects any `secret_ref` that is not empty and does
not start with `env:` or `file:`, so a credential pasted into the integration
form is refused with an explanation rather than stored.

`audit.scrub` replaces `password`, `password_hash`, `secret`, `token`,
`access_token`, `refresh_token`, `private_key` and `authorization` keys with
`[redacted]` before an audit row is written — recursively.

### Rotation

| Secret | Effect of rotation |
|--------|--------------------|
| JWT secret | All access tokens become invalid; users sign in again. No data loss. |
| Service token | Restart every service with the new value; the gateway and backends must agree. |
| Database password | Update `.env` and the PostgreSQL role, then restart. |
| Integration credential | Update the referenced environment/file; no restart needed. |
| Grafana DB password | `ALTER ROLE`, update `.env`, restart Grafana. |

Rotate all of them after any suspected exposure, and on the schedule required
by the organisation's policy.

## 7. Audit trail

Append-only, enforced by a database trigger that raises on `UPDATE` and
`DELETE`. Recorded events include:

- `auth.login` (success and failure, with reason), lockout;
- every user create/update/password reset;
- `integration.updated`, `integration.tls_verification_disabled`, `sync.requested`;
- `policy.created`, `policy.version_created`, `policy.activated`;
- `compliance.snapshot_created`, `compliance.daily_job`;
- asset exclude/include, reconciliation resolution;
- `report.requested`, report generation and delivery outcome;
- `schedule.executed`, `settings.updated`.

Each row carries actor, role, target, outcome, source IP, service and time. No
request body is stored.

## 8. Database privileges

- The application connects as a dedicated role that owns its schemas. It is not
  a superuser and does not have `CREATEDB`/`CREATEROLE`.
- Grafana connects as `patchtrack_grafana`:
  `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT`,
  `default_transaction_read_only = on`, `statement_timeout = '30s'`, and
  `SELECT` granted per table (never `GRANT ... ON ALL TABLES` to `PUBLIC`, and
  no access to `identity.*`). The role creation script requires a supplied
  password of at least 20 characters and raises if one is not provided.
- The `patchtrack_grafana` role is applied by an operator after migrations:
  `deploy/postgres/roles/010_grafana_readonly.sql`.

## 9. Container hardening

From `docker-compose.yml`, applied to every service:

| Setting | Purpose |
|---------|---------|
| `USER 1001` | non-root |
| `read_only: true` | immutable root filesystem |
| `tmpfs: /tmp` (64 MiB, mode 1777) | writable scratch without persistence |
| `security_opt: no-new-privileges` | no privilege escalation |
| `cap_drop: ALL` | no capabilities |
| json-file logging with rotation | bounded disk use |

Only the gateway (and optionally Grafana) publish a port, and by default only on
`127.0.0.1`.

## 10. SELinux

The deployment is designed to run with SELinux **enforcing**. Volume mounts are
labelled `:Z` so the container runtime relabels them for the container's
context. Nothing in this repository requires `setenforce 0`, and the
installation documentation does not instruct anyone to disable SELinux.

If a volume write fails with `EACCES` under enforcing, the fix is a label or a
`container_file_t` context — see
[troubleshooting.md](troubleshooting.md#selinux-denials).

## 11. Known security limitations

- **No MFA.** Password-only sign-in.
- **No OIDC implementation.** The schema has `auth_source='oidc'` and the login
  path refuses such accounts with a clear message, but no provider integration
  is present.
- **No automatic session revocation on role change.** A role change takes
  effect at the next sign-in (access tokens are 30 minutes).
- **JWTs are symmetric (HS256).** Every service holds the signing key. For a
  larger deployment, move to RS256/EdDSA with a key per issuer.
- **No rate limiting at the application layer** other than per-account lockout.
  Add proxy-level rate limiting for internet-facing deployments.
- **The gateway's CSRF token is not tied to the session.** It is a
  double-submit cookie, which is adequate for the same-origin deployment
  described here but would need to be bound to the session for a
  cross-domain setup.
