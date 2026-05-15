# Tasks: Dual-Environment Upload Routing (Staging & Production)

**Input**: Design documents from `/specs/004-multi-env-uploads/`
**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: REQUIRED. Constitution v3.0.0 Principle II ("Test-Driven Development — NON-NEGOTIABLE") mandates that every implementation task be preceded by a failing test committed first. The standard "tests optional" guidance does **not** apply on this project.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Different files, no dependencies on incomplete tasks → can run in parallel
- **[Story]**: Maps task to a user story from spec.md (US1–US5)
- File paths are absolute against repo root `/Users/aria/projects/pms-scanner/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: New dependencies, empty module skeletons, sample env file.

- [ ] T001 Add `ntplib>=0.4` and `types-ntplib` (dev) to `pyproject.toml`; run `pip install -e .` to confirm
- [ ] T002 [P] Create empty module files so imports resolve early: `scanner/machine.py`, `scanner/ntp.py`, `scanner/scheduler.py`
- [ ] T003 [P] Create new directory `systemd/` at repo root with a `.keep` file
- [ ] T004 [P] Update `.env.example` to match `contracts/config-schema.md` (machine identity, two `ENV_<NAME>__*` blocks, `NTP__*` block); never include real tokens

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Multi-env config, machine identity, NTP gate, state restructure, logging adapter. **No user story work can begin until this phase is complete.**

> **TDD reminder**: every `T0XX` implementation below is preceded by its failing-test sibling. Commit tests first; commit implementation only after the test fails meaningfully (not by `ImportError`).

- [ ] T005 [P] Write failing unit tests for `MachineIdentity` in `tests/unit/test_machine.py` (valid name, reserved-name rejection, blank/whitespace rejection, regex constraints, `in_progress_dir(env)` path resolution)
- [ ] T006 Implement `MachineIdentity` dataclass + validation in `scanner/machine.py` to make `tests/unit/test_machine.py` pass

- [ ] T007 [P] Write failing unit tests for multi-env config in `tests/unit/test_config_multi_env.py` covering: required-field absence, same-watch-folder rejection (FR-009), same-offset rejection (FR-006c), unknown env name rejection, `Environment.in_progress_dir(machine)` & `processed_dir` derivations, 003-era flat-var migration warning
- [ ] T008 Rewrite `scanner/config.py` into `AppSettings` containing `MachineIdentity`, `list[Environment]`, `NTPSettings` per `contracts/config-schema.md`; remove the single global `settings` instance — pass settings explicitly. Must satisfy `tests/unit/test_config_multi_env.py`

- [ ] T009 [P] Write failing unit tests for `NTPClient` in `tests/unit/test_ntp.py` (mock `ntplib.NTPClient.request`): clean offset measurement, kiss-of-death (stratum 16) → rejected, offset > 1 day → rejected, network error → `NTPUnreachableError`
- [ ] T010 Implement `NTPClient` and its result types in `scanner/ntp.py` to pass `tests/unit/test_ntp.py`

- [ ] T011 [P] Write failing integration test for NTP gate in `tests/integration/test_ntp_gate.py`: stub source returns offsets `(0.0, 0.5, 2.0, KoD)`; startup blocks scheduler registration until clean response; `abs(offset) > max_drift_seconds` → process refuses to start with a single ERROR log line naming source + measured offset (FR-022)
- [ ] T012 Implement `NTPGate` (startup) in `scanner/ntp.py` to pass `tests/integration/test_ntp_gate.py`

- [ ] T013 [P] Write failing unit tests for `DriftMonitor` in `tests/unit/test_ntp.py`: recurring check; over-threshold drift triggers helper invocation with configured command; helper non-zero exit → `WARNING` log + state's `last_drift_warning` populated; helper missing → same warning path; helper success → `drift_corrected` outcome (FR-023, FR-024)
- [ ] T014 Implement `DriftMonitor` background thread in `scanner/ntp.py` to pass `tests/unit/test_ntp.py`

- [ ] T015 [P] Write failing unit tests for thread-safe per-(machine, env) state in `tests/unit/test_state.py`: `BatchRunState` keyed by env name; per-env counters move independently; `ClockSyncEvent` storage; concurrent mutation under `RLock` does not lose updates
- [ ] T016 Restructure `scanner/state.py` per `data-model.md` (PerEnvRunState dataclass, BatchRunState containing `dict[str, PerEnvRunState]`, ClockSyncEvent record, RLock) to pass `tests/unit/test_state.py`

- [ ] T017 Add a `LoggerAdapter` factory in `scanner/state.py` (or new `scanner/logging_setup.py` if preferred) so every emit carries `env=…` and `machine=…` keyword extras; redact `api_token` always (Constitution IV)

- [ ] T018 Rewrite `scanner/__main__.py` wiring: load `AppSettings` → run startup validation order from `data-model.md` (machine name, distinct watch dirs, distinct offsets, NTP gate, dir creation, token non-empty) → instantiate `BatchRunState(machine)` → start dashboard → register scheduler (placeholder ok for now) → start `DriftMonitor` → trap `SIGTERM`/`SIGINT` and drain

**Checkpoint**: Foundation ready. User story phases can now begin (US1–US4 are P1, US5 is P2). US1 and US4 may proceed in parallel since they touch different modules (uploader/dashboard vs. batch.claim/recovery).

---

## Phase 3: User Story 1 — Production Scans Route to Production Backend (Priority: P1) 🎯 MVP

**Goal**: Files dropped in the production folder upload to `adg.mpsinc.io` with the production token. Manual trigger (no scheduler yet) is sufficient for MVP.

**Independent Test**: Drop a PDF in the production folder; POST `/run?environment=production`; verify every page is uploaded to `adg.mpsinc.io` and the file ends up in `<prod>/processed/`.

- [ ] T019 [P] [US1] Write failing unit tests for env-aware uploader in `tests/unit/test_uploader_per_env.py`: `upload_page(env=production, page)` posts to `env.backend_base_url + /api/scanned-images/upload` with `Authorization: Bearer <prod_token>`; never uses a hard-coded URL
- [ ] T020 [US1] Refactor `scanner/uploader.py` to accept `Environment` explicitly; remove module-level config access. Tests from T019 pass

- [ ] T021 [P] [US1] Write failing unit tests for env-aware batch claim in `tests/unit/test_batch_per_machine.py`: `claim_file(env, machine, src)` moves file to `env.in_progress_dir(machine)` only; success returns `Path`; if source vanished mid-claim, returns `None` and logs DEBUG (no exception)
- [ ] T022 [US1] Refactor `scanner/batch.py`: `BatchRunner` constructor takes `env: Environment` and `machine: MachineIdentity`; `claim_file` uses `os.rename` into `env.in_progress_dir(machine)`; `process_file` invokes per-env uploader. Tests from T021 pass

- [ ] T023 [P] [US1] Extend existing `tests/contract/test_upload_contract.py` to parametrize over production and staging `Environment` fixtures, proving identical request shape across both base URLs

- [ ] T024 [US1] Implement `POST /run?environment=<name>` in `scanner/dashboard.py` as a synchronous one-shot trigger (no APScheduler yet): looks up the env, builds a `BatchRunner`, executes a single pass against the watch dir

- [ ] T025 [P] [US1] Write failing integration test in `tests/integration/test_dual_env_run.py::test_production_only_routing`: spin up the app with both envs configured, a mock-server backend for prod (assert calls), and `dev.adg.mpsinc.io` pointed at a sentinel mock that fails the test if hit; drop a 3-page PDF into prod folder; `POST /run?environment=production`; assert exactly 3 page-uploads to prod, zero to staging, file in `<prod>/processed/`, nothing left in `in-progress/`

**Checkpoint**: US1 fully functional. The MVP is shippable — operators can already run production from a manual trigger while we add the rest.

---

## Phase 4: User Story 2 — Staging Scans Route to Staging Backend (Priority: P1)

**Goal**: Files dropped in the staging folder upload to `dev.adg.mpsinc.io`. Cross-contamination is impossible.

**Independent Test**: Drop a PDF in staging folder; trigger; verify upload destination is the staging backend and production backend received nothing.

- [ ] T026 [P] [US2] Add `tests/integration/test_dual_env_run.py::test_staging_only_routing` (mirror of T025 for staging)

- [ ] T027 [P] [US2] Add `tests/integration/test_dual_env_run.py::test_cross_routing_impossible`: configure both envs, drop one PDF in each folder, trigger both, assert every prod page hit `adg.mpsinc.io` and every staging page hit `dev.adg.mpsinc.io` — zero exchanges (SC-002)

- [ ] T028 [US2] Implement `POST /run` (no query param) in `scanner/dashboard.py` to trigger every enabled environment concurrently using the same thread pool that will later host scheduler jobs

- [ ] T029 [P] [US2] Add `tests/integration/test_dual_env_run.py::test_simultaneous_dual_env_trigger`: `POST /run` (no env) with files in both folders; assert both runs ran concurrently (overlap detected by timestamp) and each ended in its own `processed/`

- [ ] T030 [US2] Confirm `Environment.api_token` is `SecretStr` and not present in any log line — add an explicit assertion in `tests/unit/test_state.py::test_logger_redacts_secrets`

**Checkpoint**: US1 + US2 work side-by-side with manual triggers. Routing isolation is proven by tests.

---

## Phase 5: User Story 3 — Staggered, Concurrent Per-Environment Schedules (Priority: P1)

**Goal**: Each (machine, env) polls automatically on its declared offset. macmini at `:00`/`:15`, nuc at `:30`/`:45`. Same-env coalescing; cross-env concurrency.

**Independent Test**: Start the app on macmini with both envs enabled; watch four poll events fire per minute at the expected seconds without manual triggers.

- [ ] T031 [P] [US3] Write failing unit tests for `build_jobs` in `tests/unit/test_scheduler.py`: returns one `CronTrigger` per **enabled** env with `second=offset`, `minute='*'`; `max_instances=1`; `coalesce=True`; `misfire_grace_time=30`; disabled env produces no job (FR-006, FR-006b)
- [ ] T032 [US3] Implement `build_jobs(settings, state)` and `Scheduler.start/stop` in `scanner/scheduler.py` using `BackgroundScheduler` + `ThreadPoolExecutor(max(4, 2 * len(enabled_envs)))`. Tests from T031 pass

- [ ] T033 [P] [US3] Write failing unit tests for concurrent execution in `tests/unit/test_scheduler.py`: register two jobs at offsets 0 and 1; both fire within the same second; assert two distinct worker thread IDs serviced them (FR-006a)
- [ ] T034 [US3] Verify thread pool sizing in `scanner/scheduler.py` actually parallelizes; if tests show serialization, switch to `ProcessPoolExecutor` (research.md §2 had ThreadPool as the default)

- [ ] T035 [US3] Update `scanner/__main__.py`: after NTP gate passes, register scheduler jobs from `build_jobs(...)`, start scheduler, ensure SIGTERM drains pending jobs

- [ ] T036 [P] [US3] Add `tests/integration/test_dual_env_run.py::test_staggered_schedule_with_clock_freeze`: use `freezegun` (or `time-machine`) to advance wall clock; assert poll start times are within ±1s of `HH:MM:00` and `HH:MM:15` over a 10-minute simulated window (SC-007). Drop PDFs first; assert one upload per minute for the relevant env

- [ ] T037 [P] [US3] Add `tests/integration/test_dual_env_run.py::test_same_env_coalescing`: stub a production run that sleeps 70 s; while running, advance clock past two more `:00` ticks; assert at most one queued follow-up runs (not two) and the older one is the survivor; assert a single-line `INFO` log entry recording the coalesce decision (FR-006b)

**Checkpoint**: The fleet stride is observable in test runs. Real-clock verification on bare metal is part of Polish (T055).

---

## Phase 6: User Story 4 — Multi-Machine Deployment with Per-Machine Isolation (Priority: P1)

**Goal**: Two machines can safely share the same SMB watch tree. Per-machine in-progress subfolders; atomic-rename claims; per-machine crash recovery.

**Independent Test**: Run two app instances (`macmini`, `nuc`) against a shared tmp directory with 10 PDFs; verify exactly 10 processed across both, zero duplicates, peer subfolders never read.

- [ ] T038 [P] [US4] Write failing unit tests in `tests/unit/test_batch_per_machine.py::test_claim_writes_only_to_self_subfolder`: claim moves into `in-progress/<self>/`; never into bare `in-progress/` or a peer subfolder; created `in-progress/<self>/` exists with mode `0700` on Linux (skip mode check on macOS)
- [ ] T039 [US4] Refine `scanner/batch.py`'s claim path to ensure target directory creation at startup (idempotent `Path.mkdir(parents=True, exist_ok=True, mode=0o700)` once per env), then `os.rename(src, target/filename)`; on `FileNotFoundError` (peer won), log DEBUG and return `None`

- [ ] T040 [P] [US4] Write failing unit tests for crash recovery in `tests/unit/test_batch_per_machine.py::test_recover_stranded_only_own_subfolder`: seed files in both `in-progress/macmini/` and `in-progress/nuc/`; running recovery as `macmini` returns only macmini's files to watch dir; nuc's subfolder is byte-for-byte unchanged (SC-011)
- [ ] T041 [US4] Implement `recover_stranded(env, machine)` in `scanner/batch.py`: `os.listdir(env.in_progress_dir(machine))` only; rename each entry back to `env.watch_dir`; if name conflicts at destination, append `.recovered-<UTC-ISO>`; never call any function that traverses other subfolders

- [ ] T042 [US4] Update `scanner/__main__.py` to call `recover_stranded(env, machine)` for every enabled env **before** scheduler.start (FR-008)

- [ ] T043 [P] [US4] Write failing integration test in `tests/integration/test_two_machine_simulation.py`: two app instances in separate threads, each with its own `MachineIdentity` and dashboard port, both pointing at the same shared `tmp_path`-based watch dirs; drop 10 PDFs; `POST /run` on both; assert (a) total 10 files in `processed/`, (b) zero duplicates by filename, (c) each machine's `in-progress/<self>/` is empty post-run, (d) `in-progress/macmini/` was never written by the nuc instance and vice versa (SC-009, SC-010)
- [ ] T044 [US4] Tighten claim-race handling in `scanner/batch.py` if T043 reveals a window where two machines both `stat` the same file then race the rename — only one `os.rename` survives, so the second machine MUST swallow `FileNotFoundError` silently

**Checkpoint**: The fleet behavior is now correct end-to-end with manual triggers and scheduler combined. Dashboard hasn't been updated yet; logs are tagged but UI is unchanged.

---

## Phase 7: User Story 5 — Operator Visibility Per Environment (Priority: P2)

**Goal**: Dashboard, logs, and run summaries are tagged with machine + env so an operator can triage in < 30 s (SC-004).

**Independent Test**: Run with files in both envs; open `http://<machine>:8080`; confirm each file shows env, machine, and backend host; SSE stream includes `clock_sync` events.

- [ ] T045 [P] [US5] Write failing integration test `tests/integration/test_dashboard_multi_env.py::test_status_shape`: spin up dashboard against a known `BatchRunState`; assert JSON matches `contracts/dashboard-events.md` exactly (top-level `machine`, `ntp` block, `environments` keyed by name with `current_run`/`last_run`)
- [ ] T046 [US5] Update `scanner/dashboard.py::status_endpoint` to render the new shape from the restructured `BatchRunState`. Tests from T045 pass

- [ ] T047 [P] [US5] Write failing integration test `tests/integration/test_dashboard_multi_env.py::test_sse_events_tagged`: emit synthetic page events; subscribe via httpx-sse; assert every `page_done`, `file_done`, `run_started`, `run_done` event carries `env` and `machine`; after one NTP cycle, a `clock_sync` event arrived
- [ ] T048 [US5] Update `scanner/dashboard.py` SSE producer to tag every event with `env` + `machine` and to emit `clock_sync` / `clock_drift_warning` events from `BatchRunState.recent_clock_sync` and `last_drift_warning` mutations

- [ ] T049 [US5] Rewrite the dashboard HTML/JS payload served at `GET /` to render two side-by-side env panes (`production`, `staging`), each scoped to **this machine**; show machine identity + NTP status banner at top; expose backend hostname per env

**Checkpoint**: All five user stories deliver. Spec acceptance scenarios are exercised by integration tests above.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T050 [P] Update `launchd/io.mpsinc.pms-scanner.plist`: replace `WatchPaths` with `WaitForPaths` listing both env watch dirs; add `EnvironmentVariables` keys for `MACHINE_IDENTITY`, `ENVIRONMENTS`, `ENV_PRODUCTION__*`, `ENV_STAGING__*`, `NTP__*`
- [ ] T051 [P] Create `systemd/pms-scanner.service` (user unit) per `contracts/filesystem-layout.md` and `quickstart.md` §4: `Restart=always`, `RestartSec=10`, `RequiresMountsFor=/mnt/aria/ARIAscans-prod /mnt/aria/ARIAscans-staging`, `EnvironmentFile=%h/.config/pms-scanner/.env`, `ExecStart=%h/.venv/bin/python -m scanner`
- [ ] T052 [P] Add `scripts/macos/pms-scanner-correct-clock` shell script: `#!/bin/bash`, runs `sntp -sS "$1"`; chmod 755; document sudoers entry in `docs/launchd-setup.md`
- [ ] T053 [P] Add `scripts/linux/pms-scanner-correct-clock` shell script: `#!/bin/bash`, runs `chronyc makestep || timedatectl set-time "$(date -u --rfc-3339=seconds)"`; chmod 755; document sudoers entry in `docs/systemd-setup.md`
- [ ] T054 [P] Update `docs/launchd-setup.md` for dual-env config + NTP gate + optional clock-correction helper
- [ ] T055 [P] Create `docs/systemd-setup.md` covering mount via `/etc/fstab` with `_netdev,x-systemd.automount`, installing the user unit, journal inspection, helper-script install
- [ ] T056 Update `README.md`: new env-var table; supported platforms section (macOS 13+, Debian 12+/Ubuntu 22.04+); link to both setup docs; constitution v3.0.0 reference
- [ ] T057 Remove the 003-era flat-var compatibility shim from `scanner/config.py` once any internal `.env` files have migrated (track in CHANGELOG)
- [ ] T058 Run `quickstart.md` §0–§9 end-to-end on at least the macmini; mock the nuc with a second venv if hardware unavailable; record any deviations in a follow-up task
- [ ] T059 Verify quality gates pass: `ruff check .` → 0 violations; `mypy --strict scanner/` → 0 errors; `pytest --cov=scanner` reports ≥ 90 % on every changed/new module (Constitution III)
- [ ] T060 Add a CHANGELOG entry summarizing 004 + the constitution v3.0.0 amendment

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies — start immediately.
- **Foundational (Phase 2)**: depends on Setup. Blocks every US.
- **US1 (Phase 3, P1)**: depends on Foundational. Can run in parallel with US4 (different modules) once T018 is done.
- **US2 (Phase 4, P1)**: depends on US1 (shares uploader, batch refactors) — can start as soon as T022 lands.
- **US3 (Phase 5, P1)**: depends on Foundational + US1's `BatchRunner` shape (T022). Independent of US2 and US4.
- **US4 (Phase 6, P1)**: depends on Foundational. Can run in parallel with US1/US3 (different concerns).
- **US5 (Phase 7, P2)**: depends on Foundational + US1 + US2 (needs the per-env state to render).
- **Polish (Phase 8)**: depends on all desired user stories complete.

### Within Each User Story

- Failing tests committed before implementation tasks (TDD, Constitution Principle II).
- Models before services; services before endpoints.
- Inside the same module file, implementation tasks are **not** parallel even when both have `[P]` in different stories — only one editor at a time.

### Parallel Opportunities

- **Phase 1**: T002, T003, T004 in parallel (T001 first to install ntplib).
- **Phase 2**: every `[P]` test task can be written in parallel; implementation tasks `T006/T008/T010/T012/T014/T016` are sequential because each follows its test pair.
- **Phase 3–6**: once Foundational is done, US1 and US4 can be tackled by different developers (different modules: `uploader.py`+`dashboard.py` vs. `batch.py`); US3 can join after T022.
- **Phase 7 (US5)**: T045+T047 in parallel; implementation T046/T048/T049 sequential (same file `dashboard.py`).
- **Phase 8**: every `[P]` polish task can run in parallel.

---

## Parallel Example — Phase 2 Foundational

```bash
# Write all foundational failing tests in parallel:
Task: "tests/unit/test_machine.py"               # T005
Task: "tests/unit/test_config_multi_env.py"      # T007
Task: "tests/unit/test_ntp.py (NTPClient + DriftMonitor cases)"  # T009 + T013
Task: "tests/integration/test_ntp_gate.py"       # T011
Task: "tests/unit/test_state.py"                 # T015

# Then implement (sequential per-module):
Task: "scanner/machine.py"   # T006
Task: "scanner/config.py"    # T008
Task: "scanner/ntp.py"       # T010 → T012 → T014 (same file, sequential)
Task: "scanner/state.py"     # T016
```

## Parallel Example — US1 MVP

```bash
# After T022 lands, US1 testing can fan out:
Task: "tests/contract/test_upload_contract.py — env parametrization"   # T023
Task: "tests/integration/test_dual_env_run.py::test_production_only_routing"  # T025
```

---

## Implementation Strategy

### MVP First (US1 only)

1. Complete Phase 1: Setup (T001–T004).
2. Complete Phase 2: Foundational (T005–T018) — **CRITICAL, blocks every story**.
3. Complete Phase 3: US1 (T019–T025).
4. **STOP and VALIDATE**: drop a PDF in the production folder, hit `/run?environment=production`, watch it land in `adg.mpsinc.io`. SC-001 partially demonstrated.
5. Ship the MVP if pressure demands; otherwise continue.

### Incremental Delivery (recommended)

1. MVP (US1) → demo.
2. + US2 → cross-routing tests pass → demo.
3. + US3 → automatic stride → demo.
4. + US4 → two-machine simulation → demo.
5. + US5 → dashboard refresh → demo.
6. Polish + docs → PR ready.

### Parallel Team Strategy

If two developers are available after Foundational:

- Dev A: US1 → US2 → US5 (the routing + UI track).
- Dev B: US3 → US4 (the scheduler + isolation track).
- Both converge on Phase 8 polish.

---

## Notes

- `[P]` = different files, no in-flight dependencies — safe to parallelize.
- Constitution v3.0.0 Principle II is non-negotiable: every implementation task must be preceded by a failing test commit. Reviewers will check commit order at PR time.
- Coverage gate: ≥ 90 % on changed/new modules. Plan unit tests at the time you write the failing test, not after.
- Quickstart (T058) is the final acceptance ritual — if it doesn't run cleanly on macmini, the PR isn't ready.
