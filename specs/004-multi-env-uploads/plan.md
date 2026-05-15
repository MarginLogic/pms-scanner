# Implementation Plan: Dual-Environment Upload Routing (Staging & Production)

**Branch**: `004-multi-env-uploads` | **Date**: 2026-05-15 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `specs/004-multi-env-uploads/spec.md`

## Summary

Convert the single-target scanner from feature 003 into a fleet-aware, dual-environment uploader. Each running machine self-identifies (`macmini`, `nuc`), polls **both** a production and a staging watch folder on its own staggered schedule (macmini at `:00`/`:15`, nuc at `:30`/`:45`), routes pages to the correct backend (`adg.mpsinc.io` for production, `dev.adg.mpsinc.io` for staging) using per-environment credentials, and claims files into a per-machine subfolder of `in-progress/`. An NTP gate at startup and a recurring drift check keep the fleet's clocks aligned so the 15-second stride never collapses. The dashboard, logs, and run summaries are tagged with environment + machine.

## Technical Context

**Language/Version**: Python 3.12
**Primary Dependencies (existing, from 003)**: `pydantic-settings`, `python-dotenv`, `requests`, `apscheduler`, `fastapi`, `uvicorn[standard]`, `pymupdf`, `Pillow`, `pytesseract`
**Primary Dependencies (new for 004)**: `ntplib` (or `sntp` via stdlib `socket`) — query NTP source and measure offset; OS-level clock-setting delegated to a privileged helper or the host time-sync service (decision in research.md). No new HTTP-server dependencies — reuse FastAPI/SSE from 003.
**Storage**: Filesystem only. Per-environment layout, partitioned per machine:

```text
<env-root>/                         # e.g. /Volumes/aria/ARIAscans-prod (macOS) or /mnt/aria/ARIAscans-prod (Linux)
├── *.pdf                           # watch dir (one per env)
├── in-progress/
│   ├── macmini/                    # claimed by macmini, owned only by macmini
│   └── nuc/                        # claimed by nuc, owned only by nuc
└── processed/                      # terminal state, shared across machines (no race)
```

In-memory `BatchRunState` keyed by `(machine, environment)` for dashboard.
**Testing**: `pytest` + `pytest-asyncio`; `httpx` for FastAPI test client; `pytest-cov` for coverage. Existing TDD discipline from 003 carries forward.
**Target Platforms**:

- macOS 13+ (Ventura), Apple Silicon and Intel — primary, deploys via launchd `LaunchAgent` (existing 003 plist).
- Linux (NUC, Debian/Ubuntu LTS, x86_64) — new in this feature, deploys via `systemd --user` unit with `RequiresMountsFor=` on the SMB share. See **Complexity Tracking**.

**Project Type**: Background daemon + embedded web server (per machine).
**Performance Goals**: Same as 003 (upload start within 60 s of file arrival, dashboard refresh ≤ 3 s per page completion), per environment per machine. Idle period between polls is acceptable.
**Constraints**:

- Single process per machine handles both environments + dashboard.
- Atomic-rename-equivalent claim primitive over SMB (kernel-level `rename(2)` on SMB-mounted POSIX share is the planned mechanism; validated in research.md).
- No new databases. State stays in-memory; restart is recoverable via per-machine `in-progress/<machine>/` directory.
- Fleet clock skew ≤ 1 s, gated by NTP sync at startup and corrected hourly thereafter.
- No central coordinator. Machines learn nothing from each other beyond what the shared filesystem exposes.

**Scale/Scope**: 2-machine fleet, ≤ 50 PDFs/day total, ≤ 50 pages each. Designed to add a 3rd or 4th machine with new offsets later (e.g., add machine `lab`: production `:07.5` / staging `:22.5` — operator decides).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Cross-Platform Unattended Operation (macOS + Linux) | ✅ Pass | Constitution v3.0.0 explicitly admits Linux alongside macOS, with parallel deployment standards: launchd `KeepAlive` + `WaitForPaths` on Mac mini and systemd `--user` + `RequiresMountsFor=` on the NUC. The Python codebase is platform-agnostic; OS-specific behavior is confined to the clock-correction helper (research.md will document the per-OS choice) and the supervisor units. |
| II. Test-Driven Development | ✅ Required | Every new module (env-aware config, machine identity, NTP gate, per-machine claim, multi-env scheduler) follows Red → Green → Refactor. Failing-test commit MUST precede implementation commit. |
| III. Quality First | ✅ Required | `ruff` zero violations; `mypy --strict scanner/` zero errors; ≥ 90 % coverage on changed/new modules. |
| IV. Observability & Structured Logging | ✅ Required + extended | Every log entry MUST be tagged with `env=<production\|staging>` and `machine=<macmini\|nuc>`. Add structured events for: NTP sync attempt, NTP sync result (offset, source), NTP correction, clock-drift threshold breach, claim race outcomes, per-machine crash recovery. Credentials (each env's `api_token`) remain redacted. |
| V. Documentation Before PR | ✅ Required | Update `README.md`, `.env.example`, `docs/launchd-setup.md`; add `docs/systemd-setup.md` for the NUC; document the new config schema (envs, machine identity, NTP). |

## Project Structure

### Documentation (this feature)

```text
specs/004-multi-env-uploads/
├── plan.md              ← this file
├── research.md          ← Phase 0 output
├── data-model.md        ← Phase 1 output
├── quickstart.md        ← Phase 1 output
├── contracts/           ← Phase 1 output
│   ├── config-schema.md          # Per-environment + machine identity + NTP config contract
│   ├── dashboard-events.md       # SSE event schema tagged with env+machine
│   ├── filesystem-layout.md      # Per-machine in-progress subfolder contract
│   └── upload-endpoint.md        # Reference to 003's existing contract — unchanged
├── checklists/
│   └── requirements.md           # already created by /speckit.specify
└── tasks.md             ← Phase 2 output (/speckit.tasks — NOT created here)
```

### Source Code (repository root)

```text
scanner/
├── __main__.py              # Entry: load config → NTP gate → start dashboard → register per-(machine,env) scheduler jobs
├── config.py                # REWRITTEN: AppSettings with EnvironmentSettings list + MachineIdentity + NTPSettings
├── machine.py               # NEW: machine_identity validation; per-machine path resolution helpers
├── ntp.py                   # NEW: NTPClient (query offset), NTPGate (startup gate), DriftMonitor (recurring correction)
├── state.py                 # UPDATED: BatchRunState keyed by (machine, env); per-env per-machine counters
├── pdf_processor.py         # UNCHANGED behavior; called per env
├── uploader.py              # UPDATED: accepts EnvironmentSettings (backend host, token, optional req id) — no hard-coded host
├── batch.py                 # UPDATED: claim_file() writes to in-progress/<machine>/; recover_stranded() reads only own subfolder
├── scheduler.py             # NEW: build_jobs(machines_envs) — one APScheduler CronTrigger per (env, offset); concurrency policy
└── dashboard.py             # UPDATED: SSE multiplexed by (machine, env); /status returns per-(machine,env) snapshot

docs/
├── launchd-setup.md         # UPDATED: dual-env config example, machine_identity, NTP
└── systemd-setup.md         # NEW: NUC deployment via systemd --user

launchd/
└── io.mpsinc.pms-scanner.plist   # UPDATED env section: ENVIRONMENTS, MACHINE_IDENTITY, NTP_*

systemd/
└── pms-scanner.service      # NEW: NUC unit; RequiresMountsFor=/mnt/aria/ARIAscans-prod /mnt/aria/ARIAscans-staging

tests/
├── unit/
│   ├── test_config_multi_env.py       # Parse new schema; reject same-watch-folder, same-offset, blank machine_identity
│   ├── test_machine.py                # Validate machine name; build per-machine subfolder paths
│   ├── test_ntp.py                    # Offset measurement (mock NTP source); drift threshold; correction triggers
│   ├── test_scheduler.py              # CronTrigger per (env, offset); concurrent execution; same-env coalescing
│   ├── test_batch_per_machine.py      # Claim writes only to in-progress/<self>/; recover_stranded ignores peer subfolders
│   └── test_uploader_per_env.py       # Routing: prod folder → prod URL; staging folder → staging URL; never crosses
├── integration/
│   ├── test_dual_env_run.py           # Files in both folders, mocked backends, verify zero cross-routing
│   ├── test_two_machine_simulation.py # Two app instances, same shared dirs (tmp), verify exact-once + no cross-machine recovery
│   ├── test_ntp_gate.py               # Stub NTP source; startup blocks until first sync; refuses on drift > max
│   └── test_dashboard_multi_env.py    # SSE stream emits env+machine tags
└── contract/
    └── test_upload_contract.py        # UNCHANGED: existing POST /api/scanned-images/upload contract still holds for both env URLs
```

**Structure Decision**: Continue the single-project layout from 003. All new modules (`machine.py`, `ntp.py`, `scheduler.py`) are siblings inside `scanner/`. `config.py`, `uploader.py`, `state.py`, `batch.py`, `dashboard.py` evolve in place — no parallel "v2" tree. Linux NUC support is purely deployment-layer (`systemd/`); Python code is cross-platform.

## Complexity Tracking

| Violation / Deviation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|--------------------------------------|
| **In-process clock correction (FR-023) potentially requires elevated privileges** | When the host time-sync daemon is unavailable/disabled, the spec mandates that drift > threshold MUST be corrected, not silently tolerated. | Pure "verify only, never set" — spec FR-023 explicitly forbids silently continuing on a drifting clock. Delegate to OS time-sync service when present; only fall back to direct adjustment via a narrowly scoped privileged helper (per Linux Deployment Standards) when delegation is impossible. Mechanism choice deferred to research.md. |
| **Dashboard remains in-process despite cross-platform deployment** | Inherited from 003; carrying forward keeps the surface unchanged. SSE state is in-memory and per-machine; cross-machine aggregation is out of scope. | Centralized dashboard — would require a coordinator service or a shared store, which the spec explicitly excludes ("no central coordinator"). |

## Phase 0 — Outline & Research (deliverable: research.md)

Open questions to resolve before Phase 1:

1. **Atomic claim over SMB** — Is `os.rename` (POSIX rename) atomic across machines on an SMB-mounted share for macOS 13+ and Linux Debian/Ubuntu LTS clients? If not under any code path, what fallback (lock file, advisory lock, `O_EXCL` create marker) is portable across both?
2. **APScheduler concurrency model** — `BackgroundScheduler` vs `AsyncIOScheduler`; per-job executor selection (thread pool sized N=number of envs × 2) to satisfy FR-006a (concurrent across envs) and FR-006b (no overlap within same env, via `max_instances=1` + `coalesce=True`).
3. **NTP query and correction mechanism per OS** — `ntplib` for the query is portable. For correction: macOS has `sntp -sS` (requires `sudo`); Linux has `chronyc makestep` or `timedatectl set-time` (privileged). Decide whether to (a) delegate to host time-sync service via a polled "is it within drift?" check, or (b) call a privileged helper. Document the chosen path per OS and the install-time privilege grant.
4. **Multi-mount SMB strategy** — Production and staging watch folders may be (a) two paths on the same SMB share, or (b) two separate SMB mounts. Confirm layout assumed by spec assumptions and align launchd `WaitForPaths` / systemd `RequiresMountsFor=` accordingly.
5. **Crash-recovery scope on shared share** — Confirm that listing only `in-progress/<self>/` on startup (FR-008) is safe under concurrent writes by peer machines into their own subfolders (no shared dirent enumeration races).

## Phase 1 — Design & Contracts (deliverables: data-model.md, contracts/, quickstart.md)

Will be generated after Phase 0 closes. Outline:

- **data-model.md** — Entities from the spec: `Environment`, `MachineIdentity`, `ScheduleEntry (machine, env, offset_seconds)`, `BatchRunState` (keyed by `(machine, env)`), `FileResult`, `PageResult`, `ClockSyncEvent`, `WatchFolderAssignment`. Include startup validation rules (same-watch-folder rejection, same-offset rejection, blank machine name rejection, NTP gate).
- **contracts/config-schema.md** — Concrete `.env` / TOML schema with required keys, defaults, validation rules.
- **contracts/dashboard-events.md** — SSE event types tagged with `env` and `machine`; per-(env, machine) counter snapshot for `/status`.
- **contracts/filesystem-layout.md** — Directory tree, per-machine subfolder ownership rules, atomic-claim move semantics.
- **contracts/upload-endpoint.md** — Pointer to 003's existing contract; document that it now applies independently to both backends.
- **quickstart.md** — Operator walkthrough for a 2-machine fleet bring-up.
- **CLAUDE.md update** — Add new technologies (`ntplib`, systemd-on-Linux deployment, per-machine APScheduler concurrency) inside the preserved markers, leaving prior content untouched.

## Post-Design Constitution Re-Check

Re-evaluated against Constitution v3.0.0 after `research.md`, `data-model.md`, `contracts/*`, and `quickstart.md` were written:

| Principle | Status | Confirmed by |
|-----------|--------|--------------|
| I. Cross-Platform Unattended Operation | ✅ Pass | `quickstart.md` shows both launchd (macOS) and systemd `--user` (Linux) deployments, each with mount-wait directives. `research.md` §3 confirms main process stays unprivileged on both OSes; clock-correction is an opt-in out-of-band helper. Python code is platform-agnostic. |
| II. Test-Driven Development | ✅ Pass | Every new module in `Project Structure` has a corresponding planned test (unit and/or integration). Verification plans named in research.md §1, §2, §3, §5 will be the failing tests written first. |
| III. Quality First | ✅ Pass | No new dependencies that complicate typing (`ntplib` has type stubs in `types-ntplib`). All new modules go under `scanner/` and inherit the existing ruff + mypy --strict scope. |
| IV. Observability & Structured Logging | ✅ Pass + extended | `contracts/dashboard-events.md` adds `env` + `machine` tags to every SSE event and introduces `clock_sync` / `clock_drift_warning` events. Log lines are constitutionally required to be tagged the same way (per Principle IV amendment in v3.0.0). |
| V. Documentation Before PR | ✅ Pass | `quickstart.md`, `docs/launchd-setup.md` (update planned), `docs/systemd-setup.md` (new) cover the new operational surface. `contracts/config-schema.md` is the source of truth for the env-var table that lands in `README.md`. |

**Outcome**: no new violations introduced by Phase 1 design; the Complexity Tracking table now lists only the two design-time deviations (clock-correction privilege boundary; in-process dashboard) that were already accepted pre-design.

Ready for `/speckit.tasks`.
