# Phase 1 Data Model: Dual-Environment Upload Routing

**Branch**: `004-multi-env-uploads` | **Date**: 2026-05-15
**Inputs**: [spec.md](./spec.md), [plan.md](./plan.md), [research.md](./research.md)

This document captures the entities, fields, validation rules, and state
transitions that fall out of the spec. Implementation lives in `scanner/`.

---

## Entities

### `MachineIdentity`

The self-declared name of the running host. Sourced from configuration.

| Field | Type | Validation |
|---|---|---|
| `name` | `str` | Non-empty after strip; matches `^[a-z0-9][a-z0-9_-]{0,30}$`; reserved names rejected (`in-progress`, `processed`, `..`, `.`). |

Used for:

- naming this machine's `in-progress/<name>/` subfolder under every environment;
- tagging logs, SSE events, run-summary records;
- scoping which APScheduler jobs are registered (one set per environment, all under this machine).

Validation occurs at startup. **Failure → process refuses to start (FR-015).**

---

### `Environment`

A named upload target for files arriving in a watch folder.

| Field | Type | Validation |
|---|---|---|
| `name` | `Literal["production", "staging"]` | Closed set in this feature; future-proofed by using a string but only these two are valid today. |
| `enabled` | `bool` | Defaults to `True`. When `False`, no scheduler job is registered for this env and no startup checks apply to it. |
| `watch_dir` | `pathlib.Path` | Existing, readable, writable directory. MUST be distinct from every other configured env's `watch_dir` (FR-009). |
| `backend_base_url` | `pydantic.HttpUrl` | Production default: `https://adg.mpsinc.io`; staging default: `https://dev.adg.mpsinc.io`. |
| `api_token` | `pydantic.SecretStr` | Non-empty; redacted in every log line (Constitution Principle IV). |
| `requisition_id` | `uuid.UUID \| None` | Optional per-env (FR-014). |
| `schedule_offset_seconds` | `int` | `0 ≤ x ≤ 59`. On this machine, two enabled envs MUST NOT share an offset (FR-006c). Cross-machine offset collisions are operator-managed (spec assumption). |

Derived properties:

- `in_progress_dir(machine: MachineIdentity) -> Path` → `watch_dir / "in-progress" / machine.name`
- `processed_dir -> Path` → `watch_dir / "processed"` *(shared across the fleet, terminal state)*

---

### `WatchFolderAssignment` (validation rule, not a runtime entity)

The invariant that each filesystem folder belongs to exactly one environment.
Enforced at startup by comparing `Path.resolve()` of every enabled env's
`watch_dir`. Two equal resolved paths → process refuses to start naming both
envs (FR-009).

---

### `BackendDestination` (validation rule, not a runtime entity)

The mapping `production → adg.mpsinc.io`, `staging → dev.adg.mpsinc.io`.
Encoded as defaults on `Environment.backend_base_url`; overridable via config
for tests and air-gapped deployments. **Never** computed from file contents — only
from `watch_dir` membership (FR-002, FR-003).

---

### `ScheduleEntry`

One row in the APScheduler registry for this machine.

| Field | Type | Source |
|---|---|---|
| `environment` | `Environment` | from config |
| `offset_seconds` | `int` | `environment.schedule_offset_seconds` |
| `job_id` | `str` | f`"{machine.name}:{environment.name}"` |
| `cron_expr` | `dict` | `{"second": offset_seconds, "minute": "*"}` |

Registered on `BackgroundScheduler` with `max_instances=1`, `coalesce=True`,
`misfire_grace_time=30` (per research.md §2).

---

### `NTPSettings`

Where and how often to query the NTP source, and what counts as "too much drift."

| Field | Type | Validation / Default |
|---|---|---|
| `source` | `str` | Hostname or IP; default `pool.ntp.org`. |
| `check_interval_seconds` | `int` | `≥ 60`; default `3600`. |
| `max_drift_seconds` | `float` | `> 0`; default `1.0`. |
| `correct_clock_command` | `str \| None` | Path to the privileged helper; default `/usr/local/libexec/pms-scanner-correct-clock`. `None` disables correction (verify-only mode). |
| `startup_required` | `bool` | Default `True`; setting `False` is for local dev only and emits a startup `WARNING`. |

---

### `ClockSyncEvent`

A timestamped record of every NTP measurement. Held in `BatchRunState` and
emitted to the dashboard SSE stream.

| Field | Type | Notes |
|---|---|---|
| `measured_at` | `datetime` (UTC) | Wall-clock at the time of the measurement. |
| `source` | `str` | NTP source hostname/IP. |
| `offset_seconds` | `float` | Signed offset from server time. |
| `outcome` | `Literal["ok", "drift_corrected", "drift_uncorrected", "unreachable", "rejected_kod"]` | `rejected_kod` = kiss-of-death stratum 16. |
| `correction_exit_code` | `int \| None` | Set only when correction was attempted. |

---

### `BatchRunState` (extended from 003)

In-memory state for the dashboard. Now keyed by `(machine, environment)` rather
than a single global object.

```python
@dataclass(slots=True)
class PerEnvRunState:
    machine: str
    environment: str
    current_file: str | None
    current_page: int
    total_pages: int
    files_processed: int
    pages_uploaded: int
    errors: list[ErrorRecord]
    last_run_started_at: datetime | None
    last_run_finished_at: datetime | None

@dataclass(slots=True)
class BatchRunState:
    machine: MachineIdentity
    per_env: dict[str, PerEnvRunState]   # keyed by env name
    recent_clock_sync: ClockSyncEvent | None
    last_drift_warning: ClockSyncEvent | None
```

Thread-safety: a single `threading.RLock` on `BatchRunState` guards every
mutation. Reads for SSE are made under the lock.

---

### `FileResult` / `PageResult`

Carried forward from 003 unchanged structurally, with an added `environment`
field. (`FileResult.environment`, `PageResult.environment` are both required.)

---

## State transitions: file lifecycle (per environment, per machine)

```
[watch_dir/<file>.pdf]
        │  os.rename atomically, FR-017
        ▼
[watch_dir/in-progress/<machine>/<file>.pdf]
        │  per-page render + OSD rotation
        │  uploader.post(env, page) → env.backend_base_url
        │  on every-page success
        ▼
[watch_dir/processed/<file>.pdf]                  (terminal; shared)
```

Recovery branch (startup-only): any file found in `in-progress/<self-machine>/`
is moved back to `watch_dir/` for re-processing this run (FR-008). Peer
subfolders are never read.

---

## Startup validation order (everything below must pass)

1. `MachineIdentity.name` parses and is non-empty.  *(FR-015)*
2. Every enabled `Environment` has a unique `watch_dir.resolve()`.  *(FR-009)*
3. Every pair of enabled `Environment` schedule offsets is distinct.  *(FR-006c)*
4. NTP query against `NTPSettings.source` returns a non-KoD response within
   `ntp_startup_timeout_seconds` (default 30 s; see SC-013).  *(FR-024)*
5. `abs(offset_seconds) ≤ NTPSettings.max_drift_seconds`.  *(FR-022)*
6. `watch_dir`, `in-progress/`, `in-progress/<self-machine>/`, `processed/`
   exist or can be created. `in-progress/<self-machine>/` is created with
   mode `0700` (Linux) / default-umask (macOS).
7. Each `api_token` is non-empty.  *(Constitution IV, secrets redacted in logs)*

Any failure aborts startup with a single-line `ERROR` log naming which check
failed and the offending value (token never printed).

---

## Cross-references to spec FRs

| FR | Where it lives in this model |
|---|---|
| FR-001 / FR-002 | `Environment`, derived `backend_base_url` per env |
| FR-003 | Hardcoded routing via `Environment` membership; no global "current env" |
| FR-004 | `Environment.enabled` |
| FR-005 | `Environment.api_token` (`SecretStr`) |
| FR-006 / FR-006a / FR-006b | `ScheduleEntry` + scheduler config (research.md §2) |
| FR-006c | Startup validation step 3 |
| FR-007 | Derived `in_progress_dir(machine)` per env |
| FR-008 | Recovery branch reads only `in-progress/<self>/` |
| FR-009 | Startup validation step 2 |
| FR-010 | Per-(machine,env) state means failure of one env's job doesn't touch another's |
| FR-011 / FR-016 | Log adapter + `PerEnvRunState` tags |
| FR-012 / FR-019 | `PerEnvRunState` counters, per-machine scoped |
| FR-013 | Same `pdf_processor` invoked per env |
| FR-014 | `Environment.requisition_id` |
| FR-015 | `MachineIdentity` validation |
| FR-017 | `os.rename` atomic claim into `in_progress_dir(self)` |
| FR-018 | Recovery + scheduler ignore peer subfolders |
| FR-020 / FR-021 | `NTPSettings.source`, `check_interval_seconds` |
| FR-022 | Startup validation step 5 |
| FR-023 | `ClockSyncEvent` + correction helper invocation |
| FR-024 | Outcomes `unreachable`, `rejected_kod` map to log-and-continue vs refuse-to-start |
