# Feature Specification: Dual-Environment Upload Routing (Staging & Production)

**Feature Branch**: `004-multi-env-uploads`
**Created**: 2026-05-15
**Status**: Draft
**Input**: User description: "we want to provide support to two environments, staging and production. Each is monitoring a different folder where the uploads will go to. Based if the scanned image shows up in the staging folder, then it will upload to dev.adg.mpsinc.io and if it is uploaded to production folder, then it will upload to adg.mpsinc.io"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Production Scans Route to Production Backend (Priority: P1)

A clinic operator drops scanned PDFs into the **production** watch folder. The system processes those files and uploads every page to the production backend at `adg.mpsinc.io`. Operators do not have to change any setting, flip any switch, or choose a target — the destination is determined entirely by which folder the file landed in.

**Why this priority**: The production pipeline is the live, billed clinical workflow. Without correct routing it cannot run safely alongside staging, so this is the foundation on which Story 2 depends.

**Independent Test**: Drop a multi-page PDF into the production folder, trigger a scheduled run, and confirm in the production backend that every page from that PDF arrived — and that **no** copy of those pages landed in the staging backend.

**Acceptance Scenarios**:

1. **Given** a PDF is placed in the production watch folder, **When** the scheduled run executes, **Then** every page of that PDF is uploaded to `adg.mpsinc.io` and **no** page is uploaded to `dev.adg.mpsinc.io`.
2. **Given** the production backend is reachable but the staging backend is offline, **When** a production-folder file is processed, **Then** the run completes successfully without being affected by staging's availability.
3. **Given** a production-folder upload succeeds, **When** post-processing runs, **Then** the file is moved to a production-scoped `processed/` location — never mixed with staging's processed files.

---

### User Story 2 - Staging Scans Route to Staging Backend (Priority: P1)

A developer or QA tester drops test PDFs into the **staging** watch folder. The system uploads every page to the staging backend at `dev.adg.mpsinc.io`, using staging credentials. Test traffic never reaches the production system.

**Why this priority**: Without an isolated staging path, testing new builds means risking production data corruption. Equal priority to Story 1 because the two together form the safety guarantee — production and staging cannot share a destination.

**Independent Test**: Drop a multi-page PDF into the staging folder, trigger a scheduled run, and confirm every page arrived at `dev.adg.mpsinc.io` — and that the production backend received nothing from this run.

**Acceptance Scenarios**:

1. **Given** a PDF is placed in the staging watch folder, **When** the scheduled run executes, **Then** every page of that PDF is uploaded to `dev.adg.mpsinc.io` and **no** page is uploaded to `adg.mpsinc.io`.
2. **Given** both watch folders contain files when each environment's poll fires, **When** the production poll fires at the top of a minute and the staging poll fires 15 seconds later, **Then** staging files upload to `dev.adg.mpsinc.io` with staging credentials and production files upload to `adg.mpsinc.io` with production credentials — with no cross-contamination of files, credentials, or destinations.
3. **Given** staging credentials are invalid or expired, **When** the staging environment is processed, **Then** the staging run fails with a clear, environment-tagged error and the production environment continues processing uninterrupted.

---

### User Story 3 - Staggered, Concurrent Per-Environment Schedules (Priority: P1)

`macmini` and `nuc` run completely independently — neither machine waits for, signals, or coordinates with the other. Each machine handles **both** environments on its own clock, polling once per minute per environment at fixed seconds within the minute. The result is a clean 15-second stride across the fleet, followed by 15 seconds of fleet-wide idle time before the next minute begins:

| Second within each minute | Machine | Environment | Notes |
|---|---|---|---|
| `:00` | `macmini` | production | start of macmini's minute |
| `:15` | `macmini` | staging | macmini's last poll of this minute; macmini then idles until `:00` of the next minute |
| `:30` | `nuc` | production | start of nuc's minute |
| `:45` | `nuc` | staging | nuc's last poll of this minute; nuc then idles until `:30` of the next minute |

So `macmini` fires twice (at `:00` and `:15`) and is idle from `:15` to `:00` of the next minute. `nuc` fires twice (at `:30` and `:45`) and is idle from `:45` to `:30` of the next minute. Each machine's idle gap belongs to **that machine only** — the other machine is busy during it. When two polls overlap (e.g., a long `macmini` staging poll still running at `:30`), they execute concurrently across machines because the machines are separate hosts and concurrently within a machine because each environment runs in its own worker.

**Why this priority**: Equal P1 with Stories 1 and 2. The staggered + concurrent model is what makes "two environments across a small fleet" actually behave like four independent pipelines instead of one shared queue. Without it, a second environment on a machine would have to wait for the first, and a second machine's polls would collide with the first's.

**Independent Test**: With both machines running and files queued in both folders, observe (via log timestamps or dashboard) that the four polls fire at `:00`, `:15`, `:30`, and `:45` within each minute, that `macmini` does nothing between `:15` and the next minute's `:00`, and that `nuc` does nothing between `:45` and the next minute's `:30`.

**Acceptance Scenarios**:

1. **Given** both watch folders contain files and both machines are running, **When** the wall clock reaches `HH:MM:00`, **Then** `macmini` begins its production poll; **When** the wall clock reaches `HH:MM:15`, **Then** `macmini` begins its staging poll; **When** `HH:MM:30` fires, **Then** `nuc` begins its production poll; **When** `HH:MM:45` fires, **Then** `nuc` begins its staging poll.
2. **Given** `macmini`'s `:15` staging poll has finished, **When** the wall clock advances from `HH:MM:15` through `HH:MM:59` and into `HH:(MM+1):00`, **Then** `macmini` performs no further work until `HH:(MM+1):00`, when its next production poll fires.
3. **Given** `nuc`'s `:45` staging poll has finished, **When** the wall clock advances from `HH:MM:45` through `HH:(MM+1):29`, **Then** `nuc` performs no further work until `HH:(MM+1):30`, when its next production poll fires.
4. **Given** `macmini`'s `:15` staging poll is still uploading pages, **When** `HH:MM:30` arrives, **Then** `nuc` begins its production poll without waiting for `macmini` — the two machines run concurrently.
5. **Given** `macmini`'s `:00` production poll is still uploading pages, **When** `HH:MM:15` arrives, **Then** `macmini`'s staging poll starts in its own worker without waiting for production to finish on the same machine.
6. **Given** an administrator changes one (machine, environment) offset in that machine's configuration, **When** that machine restarts, **Then** only that pair polls on the new schedule; the other three pairs across the fleet are unaffected.
7. **Given** only one environment is enabled on a machine, **When** scheduled times fire, **Then** only that environment's poll runs on that machine — the disabled environment is never invoked.

---

### User Story 4 - Multi-Machine Deployment with Per-Machine Isolation (Priority: P1)

Two or more machines (e.g., a Mac mini and a Linux NUC) share the same network-mounted `ARIAscans` watch folders for production and staging. Each machine identifies itself by name in its own local configuration — for example, `macmini` on one host and `nuc` on the other. When a machine claims a file for processing, it moves that file into its **own** per-machine subfolder of the relevant `in-progress/` directory — e.g., `…/in-progress/macmini/` or `…/in-progress/nuc/`. Each machine also has its own staggered schedule offsets per environment, so the fleet polls in a coordinated stride without any central coordinator:

- `macmini`: production at `:00`, staging at `:15`
- `nuc`: production at `:30`, staging at `:45`

**Why this priority**: This is the only scheme that lets multiple machines share a single network-mounted scan area safely. Without per-machine isolation, two machines could try to process the same file, or one machine's crash recovery could yank in-flight files out from under another machine.

**Independent Test**: Run two machines (`macmini` and `nuc`) against the same shared watch folder structure. Drop 10 files into the production folder. Verify: every file is processed exactly once across the two machines combined; each machine's claimed files appear under that machine's own `in-progress/<machine>/` subfolder while processing; and no file ever appears in both machines' subfolders simultaneously.

**Acceptance Scenarios**:

1. **Given** the macmini's configuration declares its machine name as `macmini`, **When** it claims a file from the production watch folder, **Then** the file is moved into `in-progress/macmini/` — never into `in-progress/nuc/` and never into the bare `in-progress/` root.
2. **Given** two machines are running against the same shared watch folder, **When** both poll on their respective schedules, **Then** every file in the watch folder is processed by exactly one machine — never both and never neither (assuming both machines remain healthy).
3. **Given** the `nuc` machine crashes mid-run with files stranded in `in-progress/nuc/`, **When** `nuc` restarts, **Then** it recovers **only** its own stranded files and does **not** touch any files in `in-progress/macmini/` (which may currently belong to a live `macmini` run).
4. **Given** production is configured across the fleet with staggered offsets, **When** the wall clock crosses each minute, **Then** `macmini` polls production at `:00`, `nuc` polls production at `:30`, `macmini` polls staging at `:15`, and `nuc` polls staging at `:45` — each machine running on its own clock without coordination.
5. **Given** a machine's configuration is missing the machine name or sets it to a blank value, **When** the system starts, **Then** it refuses to start with a clear error rather than silently defaulting to a shared name (which would defeat isolation).

---

### User Story 5 - Operator Visibility Per Environment (Priority: P2)

An operator opens the live progress dashboard during a run and can see, at a glance, which environment each file is being processed in. Logs and progress entries are labeled so that a problem in one environment can be diagnosed without confusion about which backend is involved.

**Why this priority**: Without per-environment labeling, a failing staging file looks identical to a failing production file in logs and the dashboard, making triage error-prone. Important, but the upload routing in P1 must be correct first.

**Independent Test**: Start a run with files in both folders; verify the dashboard shows each file annotated with its environment (e.g., "staging" or "production") and that the run summary breaks results out per environment.

**Acceptance Scenarios**:

1. **Given** files are processing in both environments across multiple machines, **When** an operator views the dashboard, **Then** each in-progress file shows which environment it belongs to, which machine is processing it, and which backend host its pages are being uploaded to.
2. **Given** a run has completed on a given machine, **When** an operator views the run summary, **Then** the summary reports per-environment counts (files processed, pages uploaded, errors) scoped to that machine — not just a combined total.
3. **Given** an error occurs uploading a page, **When** the operator reads the log entry, **Then** the entry identifies both the environment and the machine, so the operator knows which backend, credentials, and host are involved.

---

### Edge Cases

- The **same filename** appears in both folders simultaneously — both must process independently to their own backends; neither claims the other.
- A file is moved or copied **from one watch folder to the other** between runs — it is processed by whichever folder it currently sits in, and uploads to that environment's backend.
- One environment is **fully unconfigured** (e.g., only production folder is set up, staging is not used at this site) — the system runs cleanly using only the configured environment and does not error or warn on the absent one.
- A backend is **unreachable** for one environment but reachable for the other — the reachable environment continues processing; the unreachable one's files remain unprocessed (recoverable on a later run) and clearly flagged in logs.
- An environment's `in-progress/` directory contains files left from a prior crash — crash recovery (FR-016 in feature 003) runs **independently per environment**, returning each environment's stranded files to that environment's watch folder, never crossing environments.
- A file is **invalid or unreadable** in one environment — it fails for that environment only; the other environment's processing is unaffected.
- The two environment configurations accidentally **point to the same watch folder** — this is a misconfiguration that the system MUST refuse to start with, since it would create ambiguous routing.
- A **prior poll for the same environment is still running** when the next minute's offset fires — that environment skips the new firing (or queues at most one) rather than launching a second overlapping poll against the same folder; the *other* environment's schedule is not affected.
- Two environments are configured with the **same schedule offset** (e.g., both at `:00`) — the system MUST refuse to start with a clear message, since coincident polls defeat the staggering goal.
- The system clock **jumps** (NTP adjustment, daylight-saving change) — each environment continues to fire on its declared offset; missed firings are not retroactively replayed.
- The **NTP source is unreachable at startup** (network down, server outage, firewall) — the system refuses to start (FR-024) because running with an unverified clock could break the fleet stride.
- The **NTP source goes unreachable mid-run** — the system continues on the last-known good clock and logs a warning until the next successful sync (FR-024).
- The **local clock has drifted beyond the allowable threshold** since the last sync — the system corrects the clock and logs the correction; if correction itself fails (e.g., insufficient privileges), the system halts further scheduling on that machine and surfaces a clear error.
- The **NTP source returns an obviously wrong time** (e.g., a date in 1970 or far in the future, or a value disagreeing with prior observations by hours) — the system rejects the response and logs it; if no valid response can be obtained, the recurring check is treated as a failure per FR-024.
- **Two machines are accidentally configured with the same machine name** — each will write into the same `in-progress/<name>/` subfolder, breaking isolation. The system cannot detect this across machines, but the atomic claim of FR-017 still prevents double-processing of any single file; the symptom is mixed-up crash-recovery behavior, and operators must treat machine-name uniqueness as a deployment-level invariant.
- **Two machines pick the same offset for the same environment** (e.g., both at `:00` for production) — claim atomicity (FR-017) still prevents data corruption, but throughput drops to one machine because the loser of each claim race finds nothing to do. This is a deployment-level configuration concern, not enforceable from a single machine.
- A machine's network mount **disappears mid-run** — that machine's in-flight files remain in its own `in-progress/<machine>/` subfolder on the share and are recovered the next time *that machine* successfully restarts and connects; other machines must not "rescue" them.
- An operator manually moves a file out of another machine's `in-progress/<machine>/` subfolder back to the watch folder — that file will be re-claimed by whichever machine wins the next claim, which may or may not be the original owner; this is acceptable behavior and not a bug.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST support two named upload environments — `production` and `staging` — each with its own watch folder path and its own backend destination.
- **FR-002**: System MUST route every scanned page to the backend belonging to the environment whose watch folder the source file came from. Files in the production folder upload to `adg.mpsinc.io`; files in the staging folder upload to `dev.adg.mpsinc.io`.
- **FR-003**: System MUST NOT, under any circumstance, upload a file from the production folder to the staging backend, nor a file from the staging folder to the production backend.
- **FR-004**: System MUST allow each environment to be independently enabled or disabled via configuration, so a site can run with only one environment if desired.
- **FR-005**: System MUST hold separate credentials (API token / authentication material) per environment and use only that environment's credentials when uploading its files.
- **FR-006**: System MUST run each environment on its own independent schedule, declared in each machine's local configuration. The default cadence is once per minute per environment. Default offsets are: `macmini` polls production at `:00` and staging at `:15`; `nuc` polls production at `:30` and staging at `:45`. Every offset MUST be overridable per machine, per environment.
- **FR-006a**: System MUST allow the two environments' polls to **execute concurrently** — a long-running poll for one environment MUST NOT delay, block, or skip the other environment's scheduled firing.
- **FR-006b**: System MUST guard against **overlapping polls for the same environment**: if an environment's prior poll is still running when its next scheduled firing arrives, the system MUST NOT start a second concurrent poll for that environment; the new firing is either skipped (next chance at the following minute) or coalesced into at most one queued follow-up, with the choice logged.
- **FR-006c**: System MUST refuse to start if two enabled environments are configured with the **same schedule offset**, naming both environments in the failure message, since coincident firings defeat the staggering guarantee.
- **FR-007**: System MUST keep per-environment file movement isolated: each environment has its own `in-progress/` and `processed/` locations scoped under that environment's watch folder, and files never move between environments. Within each environment's `in-progress/`, every running machine MUST claim files into its **own machine-scoped subfolder** named for its configured machine identity (e.g., `in-progress/macmini/`, `in-progress/nuc/`) — never into the bare `in-progress/` root and never into another machine's subfolder.
- **FR-008**: System MUST perform crash recovery (returning stranded `in-progress/` files to the watch folder) independently per environment AND per machine on startup. A machine's recovery MUST touch **only** its own `in-progress/<machine>/` subfolder and MUST NOT read, move, or delete files from any other machine's subfolder — those may belong to a currently-live peer.
- **FR-009**: System MUST refuse to start if any two configured environments point to the same watch folder, since routing would be ambiguous; the failure message MUST name the conflicting environments.
- **FR-010**: System MUST treat a per-environment backend failure as scoped to that environment only — other environments continue to process and upload normally during the same run.
- **FR-011**: System MUST tag every log entry and dashboard progress record with the environment name so an operator can tell at a glance whether a given event relates to staging or production.
- **FR-012**: System MUST report per-environment counts in the run summary (files processed, pages uploaded, errors), in addition to or in place of combined totals.
- **FR-013**: System MUST preserve all behavior from prior features (page orientation correction, page-by-page progress reporting, settle-time skipping, retry on transient upload failure) on a per-environment basis — every environment gets the same processing quality.
- **FR-014**: System MUST allow an environment to declare an optional default requisition link separately from the other environment, so test traffic and production traffic are not forced to share a link target.
- **FR-015**: System MUST be configured with a non-empty, self-declared **machine identity** (e.g., `macmini`, `nuc`). The system MUST refuse to start if the machine identity is missing, blank, or contains characters illegal in a directory name; the failure message MUST name which configuration field is missing or invalid.
- **FR-016**: System MUST tag every log entry, dashboard progress record, and run-summary line with the machine identity in addition to the environment name, so an operator viewing the dashboard from any machine can tell which host owns each in-flight file.
- **FR-017**: System MUST claim files into its per-machine subfolder using an operation that prevents two machines from successfully claiming the same file. If two machines attempt to claim the same file at the same time, exactly one MUST succeed and the other MUST observe the file as already taken and move on without error.
- **FR-018**: System MUST tolerate other machines' per-machine subfolders existing inside the shared `in-progress/` directory: their presence MUST NOT cause this machine to log warnings, attempt cleanup, or treat its environment as misconfigured.
- **FR-019**: Per-environment counts in the run summary (FR-012) MUST be scoped to this machine's contribution to that environment — a machine reports what *it* processed, not the fleet total.
- **FR-020**: System MUST keep the host machine's wall clock synchronized to a designated NTP time source. Synchronization MUST occur **before the first scheduled poll fires** at startup and MUST recur at a configurable interval thereafter (default: every hour).
- **FR-021**: The NTP source MUST be configurable per machine. The default MUST be a publicly available NTP source (e.g., `pool.ntp.org`) and the operator MUST be able to override it to an internal NTP server when the deployment is air-gapped or policy-restricted.
- **FR-022**: System MUST measure local clock offset against the configured NTP source at startup and reject startup if the offset exceeds a configurable **maximum allowable drift** (default: 1 second). The failure message MUST report the measured offset and the source it was measured against, so the operator can diagnose the time source rather than guess.
- **FR-023**: When the recurring synchronization check (FR-020) measures drift above the maximum allowable drift, the system MUST correct the local clock — either by triggering the host's time-sync service or by adjusting time directly — and MUST log the correction with the prior offset, the new offset, and the source used. The system MUST NOT silently continue scheduling on a drifting clock.
- **FR-024**: If the NTP source is unreachable at startup, the system MUST refuse to start (since unsynchronized clocks defeat the fleet stagger). If the NTP source becomes unreachable during a recurring check after a successful startup, the system MUST log a warning and continue running on the last-known good clock until the next successful sync — running on a stale-but-recent clock is preferable to halting an in-progress run.

### Key Entities *(include if feature involves data)*

- **Environment**: A named upload target (`production` or `staging`) with its own watch folder, backend host, credentials, and optional requisition link. Each environment owns its `in-progress/` and `processed/` sub-locations and its own progress/log namespace.
- **Watch Folder Assignment**: The mapping from a filesystem folder to exactly one environment. Each folder belongs to one and only one environment; no folder is shared.
- **Backend Destination**: The remote host that receives uploads for a given environment — `adg.mpsinc.io` for production, `dev.adg.mpsinc.io` for staging.
- **Machine**: A running host instance of the system, self-identified by a non-empty machine name declared in that host's local configuration (e.g., `macmini`, `nuc`). The machine name MUST be unique across the deployment and is used to name the machine's own `in-progress/<machine>/` subfolder under every environment, and to scope schedule offsets, logs, dashboard records, and crash recovery to this host.
- **Per-Machine In-Progress Subfolder**: A subfolder inside each environment's `in-progress/` directory, named after the machine identity, that holds files this machine has actively claimed for processing. The subfolder is owned exclusively by that machine and is the only `in-progress/` location the machine may read from or write into.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of files placed in the production folder upload to the production backend; 100% of files placed in the staging folder upload to the staging backend — measured across a verification run containing files in both folders.
- **SC-002**: Zero pages cross environments in any test run — confirmed by checking that the staging backend receives nothing from production-folder files, and vice versa.
- **SC-003**: When one backend is offline, the other environment's run completes with the same success rate as a single-environment run would (no degradation from sharing the process).
- **SC-004**: An operator can identify, in under 30 seconds from opening the dashboard, which environment any given in-flight or recently failed file belongs to.
- **SC-005**: A site administrator can enable or disable an environment with a single configuration change and restart, without touching any code or other environment's settings.
- **SC-006**: A misconfiguration where both environments point to the same watch folder is rejected at startup with a clear message naming the conflict — no silent or partial start.
- **SC-007**: Across a 10-minute observation window with files queued in both folders and both machines running, polls start within ±1 second of their assigned offsets — `macmini` production at each `HH:MM:00`, `macmini` staging at each `HH:MM:15`, `nuc` production at each `HH:MM:30`, and `nuc` staging at each `HH:MM:45`.
- **SC-008**: When both environments have work, the staging poll begins **before** the production poll has finished in at least one observed minute (proving the two are executing concurrently rather than serially).
- **SC-008a**: Across a 10-minute observation window, `macmini` records **zero** activity (no log entries, no dashboard events, no upload attempts) in the interval from each `HH:MM:15`-poll completion up to the next `HH:(MM+1):00`, and `nuc` records zero activity in the interval from each `HH:MM:45`-poll completion up to the next `HH:(MM+1):30` — confirming the per-machine idle gap.
- **SC-009**: With two machines (`macmini` and `nuc`) running against a shared watch folder containing 100 files, exactly 100 files are processed across the fleet — zero duplicates and zero losses.
- **SC-010**: At any given moment, no single file appears in more than one machine's `in-progress/<machine>/` subfolder.
- **SC-011**: A machine restarting after a crash recovers every file from its own `in-progress/<machine>/` subfolder while leaving every other machine's subfolder byte-for-byte untouched.
- **SC-012**: After 24 hours of continuous operation, the maximum observed clock skew between any two fleet machines remains **under 1 second**, measured by comparing log timestamps for the same wall-clock NTP reference.
- **SC-013**: First NTP synchronization completes (or fails the startup check) within **30 seconds** of process start, on every machine, before any scheduled poll is allowed to fire.
- **SC-014**: When an operator deliberately advances or rewinds a machine's clock by more than the allowable drift, the next recurring NTP check detects the offset and corrects it within one synchronization interval — and the correction event appears in the log with the prior and new offsets.

## Assumptions

- Within a single machine, both environments run in the same process and share that machine's dashboard. Across machines, multiple hosts (e.g., `macmini` and `nuc`) run their own independent instances against shared, network-mounted watch folders; there is no central coordinator and no shared dashboard across machines.
- Each environment has its own pre-issued API token / credential; credentials are not shared between environments.
- Operators understand the difference between staging and production and will place files in the correct folder; the system does not attempt to detect "wrong-environment" content based on file contents.
- The backend at `dev.adg.mpsinc.io` exposes the same upload contract (`POST /api/scanned-images/upload`) as `adg.mpsinc.io`, with no per-environment protocol differences.
- The existing watch folder used by feature 003 will be retained as the **production** environment's folder by default for backward compatibility; staging is additive and opt-in.
- Both environments default to enabled when configured; sites that want only one environment will explicitly disable the other via configuration.
- The default 15-second stagger between production (`:00`) and staging (`:15`) is comfortable: production polls are expected to complete in well under 15 seconds in normal operation, so staging usually starts on a quiet system. The model still supports overlap if production runs long — concurrency is not just a performance optimization, it is the correctness contract for staggered scheduling.
- The host machine has enough capacity (CPU, network, file descriptors) to run both environments' polls concurrently in the worst case. This is consistent with current single-environment hardware sizing because typical poll cycles are short and mostly I/O bound.
- Watch folders are network-mounted (e.g., SMB) and shared across machines; the `processed/` directory remains shared (terminal state, no race) while `in-progress/` is partitioned per machine for safety.
- The shared filesystem supports an atomic rename or equivalent claim primitive that lets each machine establish exclusive ownership of a file when moving it into its own `in-progress/<machine>/` subfolder; without this, the FR-017 "exactly one claim wins" guarantee cannot hold.
- Machine names are assigned and kept unique by the operator at deployment time — the system has no central registry to enforce uniqueness across hosts and instead trusts each machine's local configuration.
- Schedule offsets across the fleet are also coordinated by the operator at deployment time. Default offsets (`macmini`: production `:00` / staging `:15`; `nuc`: production `:30` / staging `:45`) give a clean 15-second stride across both machines and both environments and can be adjusted as new machines are added.
- A reachable NTP source is available to every fleet machine — either a public source such as `pool.ntp.org` or an operator-supplied internal NTP server. Without this, FR-020 cannot hold and the fleet stagger cannot be guaranteed.
- The host operating system permits the running process to adjust the wall clock (directly or through a privileged helper / time-sync service). Where this requires elevated privileges, the operator is responsible for granting them at install time; the spec does not prescribe the mechanism, only the outcome (clock stays inside the allowable drift).
- The 1-second default for maximum allowable drift is comfortably below the 15-second stride between adjacent offsets, so a single missed correction cycle cannot cause two machines' polls to collide.
