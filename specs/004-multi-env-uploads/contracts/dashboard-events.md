# Contract: Dashboard API (multi-env, multi-machine extension)

**Base URL**: `http://<this-machine-local-ip>:{DASHBOARD_PORT}` (default port: 8080, **per machine**)
**Auth**: None — open on MedPath Wi-Fi
**Provider**: `scanner/dashboard.py` (FastAPI + uvicorn)

This contract extends [003's dashboard-api.md](../../003-we-watch-all/contracts/dashboard-api.md).
**Diff from 003 is the only thing documented below**; events not listed here keep their 003 shape.

Every machine in the fleet runs its own dashboard on its own port. There is no
cross-machine aggregation (spec assumption — "no central coordinator").

---

## `GET /status` (changed)

Per-(machine, env) snapshot, plus the latest NTP record.

```json
{
  "machine": "macmini",
  "ntp": {
    "source": "pool.ntp.org",
    "last_measured_at": "2026-05-15T19:33:00Z",
    "offset_seconds": 0.043,
    "outcome": "ok",
    "last_drift_warning": null
  },
  "environments": {
    "production": {
      "enabled": true,
      "schedule_offset_seconds": 0,
      "backend_base_url": "https://adg.mpsinc.io",
      "current_run": { "...": "same shape as 003 /status.current_run" },
      "last_run": { "...": "same shape as 003 /status.last_run" }
    },
    "staging": {
      "enabled": true,
      "schedule_offset_seconds": 15,
      "backend_base_url": "https://dev.adg.mpsinc.io",
      "current_run": null,
      "last_run": { "...": "..." }
    }
  }
}
```

**Per-env `current_run` / `last_run`**: same shape as 003's `current_run` /
`last_run`, with one added top-level field: `"environment": "production"` (or
`"staging"`).

---

## `GET /events` (changed)

Every existing SSE event grows two new top-level fields: `env` and `machine`.

```
event: page_done
data: {"env":"production","machine":"macmini","run_id":"...","filename":"...","page_num":7,"total_pages":33,"success":true,"rotation_applied":90}

```

New event types added in this feature:

| Event | When | Data |
|---|---|---|
| `clock_sync` | After every NTP measurement (startup + recurring) | `{"machine":"macmini","source":"pool.ntp.org","offset_seconds":0.043,"outcome":"ok","measured_at":"..."}` |
| `clock_drift_warning` | When `outcome ∈ {drift_uncorrected, unreachable, rejected_kod}` | `{"machine":"...","source":"...","offset_seconds":-2.1,"outcome":"drift_uncorrected","correction_exit_code":1,"measured_at":"..."}` |

`heartbeat` is emitted globally (not per-env) and unchanged.

---

## `POST /run` (changed)

Now takes an optional `environment` query param to scope the manual trigger:

```
POST /run?environment=staging
```

- Omitted → trigger every enabled env on this machine concurrently.
- Specified → trigger only that env on this machine.
- Unknown env name → `404 {"detail":"environment 'xyz' not configured on this machine"}`.

Response `202`:

```json
{ "machine": "macmini", "triggered": ["staging"], "run_ids": {"staging": "..."} }
```

---

## Backwards-compat for 003 dashboard consumers

The 003 dashboard HTML / JS is rewritten to subscribe to the new shape; we do
**not** preserve the 003 wire shape behind a flag. Any custom integrations on
the old shape break at this PR and must update — the spec explicitly removes
the single-env assumption.
