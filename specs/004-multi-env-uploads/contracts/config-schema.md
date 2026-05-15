# Contract: Configuration Schema

**Branch**: `004-multi-env-uploads` | **Date**: 2026-05-15

This file is the source of truth for the new config schema. Implementation lives
in `scanner/config.py`. Tests in `tests/unit/test_config_multi_env.py`.

---

## Source format

Configuration is loaded from environment variables (with `.env` support via
`python-dotenv`), nested using the `__` separator. Sensitive fields are
`SecretStr`. The schema is enforced by `pydantic-settings`.

A TOML/YAML option is **out of scope** for this feature; reuse the existing
env-var pattern from 003 so launchd plists and systemd `Environment=` lines
remain the single source.

---

## Top-level shape

```env
# Machine identity (required)
MACHINE_IDENTITY=macmini

# NTP (required unless explicitly disabled in dev)
NTP__SOURCE=pool.ntp.org
NTP__CHECK_INTERVAL_SECONDS=3600
NTP__MAX_DRIFT_SECONDS=1.0
NTP__CORRECT_CLOCK_COMMAND=/usr/local/libexec/pms-scanner-correct-clock
NTP__STARTUP_REQUIRED=true
NTP__STARTUP_TIMEOUT_SECONDS=30

# Environments — list of enabled env names, comma-separated
ENVIRONMENTS=production,staging

# Production env block
ENV_PRODUCTION__ENABLED=true
ENV_PRODUCTION__WATCH_DIR=/Volumes/aria/ARIAscans-prod
ENV_PRODUCTION__BACKEND_BASE_URL=https://adg.mpsinc.io
ENV_PRODUCTION__API_TOKEN=<jwt or token>
ENV_PRODUCTION__REQUISITION_ID=        # optional UUID
ENV_PRODUCTION__SCHEDULE_OFFSET_SECONDS=0

# Staging env block
ENV_STAGING__ENABLED=true
ENV_STAGING__WATCH_DIR=/Volumes/aria/ARIAscans-staging
ENV_STAGING__BACKEND_BASE_URL=https://dev.adg.mpsinc.io
ENV_STAGING__API_TOKEN=<jwt or token>
ENV_STAGING__REQUISITION_ID=
ENV_STAGING__SCHEDULE_OFFSET_SECONDS=15

# Shared (carried from 003)
DASHBOARD_PORT=8080
FILE_SETTLE_SECONDS=10
UPLOAD_TIMEOUT_SECONDS=30
UPLOAD_MAX_RETRIES=3
UPLOAD_RETRY_MAX_WAIT_SECONDS=10
LOG_LEVEL=INFO
```

## Default offsets by machine

These are **machine-side** defaults that the operator sets per-host; they are
not auto-derived by the app from `MACHINE_IDENTITY`.

| Machine | `ENV_PRODUCTION__SCHEDULE_OFFSET_SECONDS` | `ENV_STAGING__SCHEDULE_OFFSET_SECONDS` |
|---|---|---|
| macmini | `0` | `15` |
| nuc | `30` | `45` |

## Validation rules (startup gate; see `data-model.md` §"Startup validation order")

1. `MACHINE_IDENTITY` parses (`^[a-z0-9][a-z0-9_-]{0,30}$`) and is not a
   reserved name. → otherwise `RuntimeError("MACHINE_IDENTITY ...")`.
2. `ENVIRONMENTS` contains only `production` and/or `staging`. Unknown env
   names are rejected.
3. For every named env: all `ENV_<NAME>__*` keys present; `WATCH_DIR` exists
   and is writable; `API_TOKEN` non-empty; `SCHEDULE_OFFSET_SECONDS ∈ [0, 59]`.
4. Resolved `WATCH_DIR` values of enabled envs are pairwise distinct.
5. `SCHEDULE_OFFSET_SECONDS` of enabled envs are pairwise distinct.
6. NTP gate passes per `data-model.md`.
7. `BACKEND_BASE_URL` parses as `https://…` (HTTP allowed only when
   `LOG_LEVEL=DEBUG` *and* a `PMS_SCANNER_ALLOW_INSECURE=1` escape hatch is set;
   defaults are HTTPS).

Failure → log a single `ERROR` line naming the offending field and the
violated rule (never the token value) and exit `1`.

## Backwards compatibility with 003

The 003 config used flat names (`WATCH_DIR`, `BACKEND_BASE_URL`, `API_TOKEN`).
A short-lived shim in `scanner/config.py`:

- If `ENVIRONMENTS` is **absent** and 003-era flat vars are present, synthesize
  a single `production` env from them (warn at startup that the user should
  migrate).
- If `ENVIRONMENTS` is present, ignore the 003-era vars entirely.

This shim is removed in a follow-up cleanup PR; do not extend it.
