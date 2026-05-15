# Phase 0 Research: Dual-Environment Upload Routing

**Branch**: `004-multi-env-uploads` | **Date**: 2026-05-15
**Inputs**: [spec.md](./spec.md), [plan.md](./plan.md), constitution v3.0.0
**Purpose**: Resolve open questions raised in `plan.md` Phase 0 so design can proceed.

---

## 1. Atomic claim over SMB

**Decision**: Use `os.rename(src, dst)` to move a file from the environment's watch
directory into `in-progress/<machine>/`. Treat a `FileNotFoundError` or `OSError`
caused by the source no longer existing as "another machine got it first" — log
at DEBUG and move on without error (FR-017).

**Rationale**: POSIX `rename(2)` is required to be atomic with respect to other
processes observing the namespace. Modern SMB clients on macOS (SMB2/3, default on
13+) and Linux (`cifs.ko` with `vers=3.0`+ in `mount.cifs`) implement rename via
`SMB2_SET_INFO` with `FileRenameInformation`, which the SMB server executes as a
single metadata operation. Two callers cannot both succeed: exactly one
observes success, the other gets `ENOENT` because the source was already moved
out from under it. This satisfies "exactly one machine wins" from FR-017 without
requiring lock files, advisory locks, or a coordinator.

**Alternatives considered**:

- **Lock files (`O_EXCL` create of `<name>.lock`)** — works portably but requires a
  second filesystem round-trip and leaves stale locks after crashes; we'd then
  need lock TTL logic. Rejected as needlessly complex when rename already
  guarantees atomicity.
- **Advisory `flock`** — `flock` semantics over SMB are unreliable across clients
  (macOS and Linux cifs differ); rejected.
- **A central claim service** — explicitly excluded by the spec ("no central
  coordinator").

**Verification plan**: integration test
`tests/integration/test_two_machine_simulation.py` runs two app instances in
threads against a shared tmp directory with `O_EXCL`-like contention (10 files,
both instances polling simultaneously); asserts exactly 10 files in total appear
once across the two `in-progress/<machine>/` subfolders.

---

## 2. APScheduler concurrency model

**Decision**: Use `apscheduler.schedulers.background.BackgroundScheduler` with a
`ThreadPoolExecutor` sized to `max(4, 2 × number_of_enabled_environments)`. One
`CronTrigger` job per *enabled* environment on this machine, configured with
`second=<offset>`, `minute='*'`, `max_instances=1`, `coalesce=True`,
`misfire_grace_time=30`.

**Rationale**:

- `BackgroundScheduler` runs the loop in its own thread without commandeering an
  asyncio loop — fits the FastAPI/uvicorn process model already in use from 003.
- `max_instances=1` + `coalesce=True` is the literal expression of FR-006b: if a
  prior run is still going when the next fires, the new firing is *coalesced*
  (at most one queued follow-up) rather than starting a second concurrent run
  for that env.
- Separate jobs per environment, each running in the thread pool, give us
  FR-006a (concurrent across envs) for free — the production job and the
  staging job dispatch from the same pool to independent threads.
- A 30 s misfire grace period absorbs short startup hiccups (e.g., SMB
  reconnect) without dropping a poll silently.
- Pool size formula handles the present 2-environment fleet and leaves margin
  for an operator adding a 3rd configured environment without re-tuning.

**Alternatives considered**:

- `AsyncIOScheduler` — would force every blocking call (PIL render,
  `requests.post`, `os.rename`) into a thread pool anyway; no real benefit
  versus added cognitive load. Rejected.
- One scheduler per environment — duplicate thread overhead, no functional gain.
  Rejected.
- `BlockingScheduler` — incompatible with the in-process FastAPI dashboard.
  Rejected.

**Verification plan**: unit tests in `tests/unit/test_scheduler.py` assert (a)
jobs register with the expected cron expression per (env, offset), (b)
`max_instances=1` is set on every job, (c) jobs from different envs dispatch to
distinct threads when fired within a few hundred ms of each other.

---

## 3. NTP query and clock correction per OS

**Decision**: Use `ntplib` (>= 0.4) to query the configured NTP source for offset
measurement on every platform. Do **not** correct the clock from inside the main
unprivileged process. Instead:

- **Default path (both platforms)**: rely on the host's existing time-sync
  service to keep the clock disciplined — `timed` on macOS (with "Set date and
  time automatically" enabled in System Settings → General → Date & Time),
  `systemd-timesyncd` or `chrony` on Linux. The pms-scanner app's job is to
  *verify* alignment, not to set the clock.
- **Verification at startup (FR-022)**: query `ntplib` against the configured
  source; if `abs(offset) > max_allowable_drift_seconds`, refuse to start with
  a message that names the measured offset, the source, and tells the operator
  to enable/repair the host time-sync service.
- **Recurring drift check (FR-020/FR-023)**: every `ntp_check_interval_seconds`
  (default 3600), re-measure offset. If drift exceeds threshold:
  - Invoke a narrowly scoped helper script (path configurable, default
    `/usr/local/libexec/pms-scanner-correct-clock`) that the operator installs
    out-of-band with the appropriate sudoers / launchd-helper privileges. The
    helper does `sntp -sS <source>` on macOS or `chronyc makestep` /
    `timedatectl set-time` on Linux.
  - If the helper is absent or returns non-zero: log a `WARNING` with the
    measured offset, source, and helper exit code, then continue on the
    last-known-good clock (FR-024's "log and continue" branch). The dashboard
    surfaces the drift warning so the operator sees it without grepping logs.
- **Obviously wrong response guard**: ignore an `ntplib` response whose stratum
  is 16 ("kiss-of-death") or whose offset magnitude exceeds 1 day; treat it as
  "unreachable" and apply the FR-024 unreachable-mid-run policy.

**Rationale**: This satisfies every NTP-related FR while keeping the main
process unprivileged on both platforms. Setting the system clock requires
elevation on every supported OS; bundling that into the main daemon would
either run pms-scanner as root (Linux constitutional violation — "MUST never
run the main process as root") or require an SMJobBless helper on macOS. The
out-of-band helper script keeps the privileged surface area tiny and lets
operators turn it off entirely on hosts where they prefer to manage time-sync
themselves.

**Alternatives considered**:

- **Always set the clock from inside the daemon** — violates "main process never
  runs as root" on Linux; rejected.
- **Verify but never correct** — directly violates FR-023 ("MUST correct"); rejected.
- **Roll our own SNTP client over `socket`** — possible but ntplib is small,
  pure-Python, and already handles the kiss-of-death/stratum quirks; rejected as
  needless reinvention.

**Verification plan**: `tests/unit/test_ntp.py` stubs ntplib with controlled
offsets (0, 0.5, 2, kiss-of-death) and asserts the gate decisions.
`tests/integration/test_ntp_gate.py` exercises a fake NTP source via a thin
stub server and verifies the startup gate prevents poll registration until a
clean measurement arrives.

---

## 4. Multi-mount SMB strategy

**Decision**: Each environment's `watch_dir` is an **independent path**, which
may be (a) a subdirectory of a single SMB mount or (b) a completely separate
SMB mount. The application doesn't care; it just opens whatever paths config
gives it. Both options are documented in `quickstart.md`.

**Supervisor configuration**: `WaitForPaths` (macOS) and `RequiresMountsFor=`
(Linux) MUST list every distinct *mount point* that any configured
`watch_dir` lives under. The deployment docs include a snippet operators can
copy that enumerates both env paths.

**Rationale**: Treating watch dirs as opaque paths keeps the application
agnostic to SMB topology. Reuse of a single mount is more convenient when both
environments share the same fileserver and credentials; separate mounts are
necessary when the staging and production shares live on different servers
(common in regulated deployments). The app supports either with zero code
difference.

**Verification plan**: documentation review (`docs/launchd-setup.md`,
`docs/systemd-setup.md`) lists both topology patterns; no code-level test
needed — the path is just a string from config.

---

## 5. Crash-recovery scope on a shared share

**Decision**: On startup, each machine performs `os.listdir(in_progress_dir /
machine_identity)` only — never the bare `in_progress_dir` and never any peer
subfolder. Every file listed is moved back to the watch dir via
`os.rename`, mirroring the claim move in reverse.

**Rationale**: Per-subdir listing is a single, well-defined operation that does
not race with peer machines writing into their *own* subfolders, because POSIX
guarantees `readdir` consistency within a single directory snapshot and the
peer is operating on a different directory entirely. Even if the SMB client
caches dirents aggressively, the worst case is "we don't see a file this
machine itself just wrote" — which is impossible because recovery only runs at
startup, before any claims have happened.

Recovery is wrapped in `try/except OSError` so a peer's concurrent
subfolder-create (e.g., the peer just started and `mkdir -p`'d its own
subfolder) doesn't break our own startup.

**Rationale corollary** (FR-018): when this machine iterates its env's
`in-progress/` directory to discover its own subfolder, it MUST ignore any
non-self subfolders rather than warn — those are peers' working areas.

**Alternatives considered**:

- **Sweep the entire `in-progress/` for stranded files at startup** — would
  delete or move files belonging to a peer that is currently running. Rejected;
  catastrophic.
- **Use a "claim lease" file with a TTL** — adds complexity, breaks if clocks
  drift past TTL, and the rename atomicity already gives us the guarantee.
  Rejected.

**Verification plan**: `tests/unit/test_batch_per_machine.py::test_recovery_only_own_subfolder`
seeds files in both `in-progress/macmini/` and `in-progress/nuc/`, runs
recovery as `macmini`, asserts only macmini's files moved back and nuc's were
left byte-for-byte untouched.

---

## Resolved unknowns recap

Every "NEEDS CLARIFICATION" candidate from plan.md Technical Context is now
resolved:

| Question | Decision |
|---|---|
| SMB atomic claim primitive | `os.rename`, treat `ENOENT` as "peer won" |
| APScheduler shape | `BackgroundScheduler` + thread pool ≥ 4; one cron job per env with `max_instances=1` + `coalesce=True` |
| NTP query library | `ntplib` (pure Python) |
| NTP correction mechanism | Out-of-band privileged helper script (`pms-scanner-correct-clock`), per OS; main process stays unprivileged |
| SMB mount topology | Application agnostic; supervisor `WaitForPaths` / `RequiresMountsFor=` lists every mount actually used |
| Crash recovery scope | List only `in-progress/<self>/`; never read peer subfolders |

Phase 1 can now generate `data-model.md`, `contracts/`, and `quickstart.md`.
