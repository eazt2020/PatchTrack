# Monitoring

Each PatchTrack service exposes Prometheus metrics at `/metrics`:

| Metric | Type | Meaning |
|--------|------|---------|
| `patchtrack_http_requests_total` | counter | Requests by service, method, route template and status |
| `patchtrack_http_request_duration_seconds` | histogram | Request latency; p95/p99 via `histogram_quantile` |
| `patchtrack_service_uptime_seconds` | gauge | Seconds since the process started |

The `route` label is the **template** (`/servers/{server_id}`), never the raw
path, so URL values cannot create unbounded series.

## Start the bundled Prometheus

```bash
docker compose --profile monitoring up -d prometheus
```

It listens on `127.0.0.1:9090` and evaluates `alerts.yml`. To deliver alerts,
add your Alertmanager endpoint under `alerting:` in `prometheus.yml` and reload
the container. Do not expose Prometheus or the service ports to untrusted
networks: the metrics endpoint has no authentication by design, because a
scraper has no user session.

## Signals still collected by query

Business signals that are not yet exported as gauges remain in the runbook:
last successful collection age, failed collection streaks and reports stuck in
`generating`. Use a SQL probe or the Operational health screen until they are
exported.
