<!--
SYNC IMPACT REPORT
==================
Version change: 2.0.0 → 3.0.0
Modified principles:
  - Principle I: "macOS-First, Unattended Operation" → "Cross-Platform Unattended Operation (macOS + Linux)"
    (extended platform scope to include Linux; supervision broadened from launchd-only to launchd-on-macOS + systemd-on-Linux; mount-wait requirement generalized — WaitForPaths on macOS, RequiresMountsFor on Linux)
Renamed/restructured sections:
  - "macOS Deployment Standards" — narrowed to macOS-specific items only (launchd, plist, WaitForPaths, mount path via Finder/System Settings)
Added sections:
  - "Linux Deployment Standards" — systemd --user unit, RequiresMountsFor, mount via /etc/fstab or systemd .mount unit, equivalent security/health requirements
  - "Cross-Platform Operational Invariants" — items that hold regardless of OS (no interactive session, env-only secrets, structured logging, /healthz)
Templates requiring updates:
  - .specify/templates/plan-template.md  ✅ no changes needed — Constitution Check section is generic
  - .specify/templates/spec-template.md  ✅ no changes needed
  - .specify/templates/tasks-template.md ✅ no changes needed
  - specs/003-we-watch-all/plan.md       ✅ remains valid (003 is macOS-only — a permitted subset of allowed platforms)
  - specs/004-multi-env-uploads/plan.md  ⚠ update Constitution Check from "Deviation accepted" to "Pass" and remove the Linux NUC row from Complexity Tracking
Follow-up TODOs:
  - In specs/004-multi-env-uploads/plan.md: replace the ⚠️ "Deviation accepted" for Principle I with ✅ "Pass" referencing v3.0.0, and delete the "Linux NUC peer" row from the Complexity Tracking table.
-->

# pms-scanner Constitution

## Core Principles

### I. Cross-Platform Unattended Operation (macOS + Linux)

This service MUST run reliably as a background process on macOS or Linux machines
without any human interaction. The same Python codebase MUST run on both platforms;
only the supervision layer and mount-wait directive differ. Specific requirements:

- The service MUST handle restarts gracefully via the host's native supervisor:
  - **macOS**: launchd `LaunchAgent` with `KeepAlive: true`.
  - **Linux**: systemd `--user` unit (or system unit where appropriate) with
    `Restart=always` and a backoff interval.
  No other process supervisor is required or supported on either platform.
- The service MUST NOT require an interactive session or GUI at any point.
- All file-system paths MUST be configurable via environment variables — no
  hard-coded paths (e.g., `/Users/someone/...`, `/Volumes/aria/...`,
  `/home/someone/...`, `/mnt/...`) in source code.
- The service MUST recover from transient I/O errors (locked files, network share
  interruptions) without crashing; retry logic with exponential back-off is REQUIRED.
- Startup and shutdown lifecycle MUST be explicitly managed: log startup success,
  trap SIGTERM/SIGINT, and flush in-flight uploads before exit.
- The supervisor configuration MUST delay startup until the watch volume(s) are
  mounted:
  - **macOS**: launchd plist `WaitForPaths` listing every watch directory.
  - **Linux**: systemd unit `RequiresMountsFor=` (or a corresponding `.mount` unit
    dependency) listing every watch directory.
- The Python code itself MUST remain platform-agnostic; OS-specific behavior is
  permitted only in clearly bounded modules (e.g., a clock-setting helper) and
  MUST be feature-detected, never branched on string-matched platform names where
  a capability check is possible.

**Rationale**: An unattended service that crashes silently causes data loss.
Reliability and self-recovery are non-negotiable for a production background
process running on shared office infrastructure. Cross-platform support enables a
heterogeneous fleet (e.g., Mac mini + Linux NUC) to share workload without
operational surprises; uniformity of behavior across platforms is enforced by
shared Python code and matched supervisor semantics.

### II. Test-Driven Development (NON-NEGOTIABLE)

TDD MUST be followed without exception on every change:

1. Write the failing test first — it MUST fail before any implementation is written.
2. Implement only the minimum code required to make the test pass (Red → Green).
3. Refactor under green (Red → Green → Refactor cycle strictly enforced).
4. No production code may be merged to `main` without a corresponding test that
   was committed before the implementation.

Test pyramid for this project:

- **Unit tests**: Pure logic — config parsing, retry calculation, file-settle logic.
- **Integration tests**: Batch runner lifecycle, HTTP upload against a local mock server.
- **Contract tests**: API contract against the backend upload endpoint schema.

**Rationale**: TDD prevents regressions, forces clear interface design before
implementation, and is the primary quality gate for an unattended service where
runtime failures are not immediately visible.

### III. Quality First

Code quality is a first-class constraint, not an afterthought:

- All code MUST pass `ruff` (linting + formatting) with zero violations.
- All code MUST pass `mypy --strict` type checking with zero errors.
- Test coverage MUST remain ≥ 90% on all non-trivial modules.
- No `TODO` or `FIXME` comments may be merged to `main` without a linked issue.
- Functions with cyclomatic complexity > 10 MUST be refactored or explicitly justified
  before merge.

**Rationale**: This service runs unattended; defects discovered at 3 AM are expensive
to diagnose and fix. Automated quality gates catch issues before they reach production.

### IV. Observability & Structured Logging

Every meaningful runtime event MUST be logged at the appropriate level:

- Use Python's `logging` module with the standard structured format:
  `%(asctime)s [%(levelname)s] %(name)s: %(message)s`
- Log startup configuration (secrets such as `api_token` MUST be redacted — never
  logged in plain text).
- Log every file detected, upload attempt, upload outcome (success/failure), and retry.
- Errors MUST include context: file path, HTTP status code, and exception message.
- Log level MUST be configurable via `LOG_LEVEL` environment variable (default: `INFO`).
- Where a deployment involves multiple environments (e.g., staging, production) or
  multiple machines, every log entry MUST be tagged with the environment and machine
  identity so events can be triaged without ambiguity about source.

**Rationale**: When a service runs unattended, logs are the only window into its
behaviour. Insufficient logging means silent data loss with no path to diagnosis.

### V. Documentation Before PR

Documentation MUST be current and accurate before any pull request is submitted for
review:

- `README.md` MUST reflect any new or changed environment variables, configuration
  options, or operational procedures introduced by the PR.
- Any API contract changes or deployment procedure changes MUST be documented in
  the relevant `docs/` file before the PR is opened.
- The PR description MUST include a confirmed "Docs updated" checklist item.
- Reviewers MUST reject PRs where documentation is demonstrably out of date or
  missing for user-visible changes.

**Rationale**: Unattended services are operated by people who were not present at
implementation time. Accurate, current documentation is the only operational guide
available without interrupting the original author.

## Cross-Platform Operational Invariants

These apply on **every** supported platform; platform-specific sections below
augment but do not override them.

- **No interactive session, ever**: the service MUST not require a logged-in user,
  a TTY, or a GUI. Headless boot to running state is the expected lifecycle.
- **Secrets via environment only**: `api_token` and every other credential MUST
  be supplied through environment variables. Secrets MUST NOT be committed to
  source control, baked into supervisor units, or written to log output.
- **Health endpoint**: a lightweight `GET /healthz` HTTP endpoint MUST be provided
  by every running instance so operators can confirm the service is alive without
  parsing logs.
- **Path config**: every filesystem path the service touches MUST be configurable;
  no hard-coded user, mount, or volume paths.
- **Time discipline**: where scheduling correctness depends on the wall clock
  across multiple machines, NTP synchronization MUST be required by the service
  itself (verified at startup, corrected during operation) — not implicitly
  trusted to the OS alone.

## macOS Deployment Standards

- **Supported hosts**: macOS 13 (Ventura) or later; Apple Silicon (arm64) and Intel (x86_64).
- **Service management**: launchd `LaunchAgent` with `KeepAlive: true` is the required
  deployment method on macOS. The plist lives in `~/Library/LaunchAgents/`. No Docker,
  NSSM, or other supervisors are used or supported on macOS.
- **SMB mount dependency**: The launchd plist MUST include a `WaitForPaths` entry for
  every watch volume (e.g., `/Volumes/aria/ARIAscans-prod`, `/Volumes/aria/ARIAscans-staging`)
  so the service does not start until the network share(s) are mounted.
- **Volume mapping**: Watch directories MUST be SMB shares mounted via Finder or
  System Settings → Login Items; the mount paths MUST be documented in `README.md`.

## Linux Deployment Standards

- **Supported hosts**: Debian 12+ or Ubuntu 22.04 LTS+ on x86_64 (NUC-class hardware
  is the reference platform).
- **Service management**: systemd `--user` unit (preferred) or system unit where
  necessary, with `Restart=always` and a `RestartSec=` backoff (5–30 s recommended).
  No Docker, supervisord, init.d scripts, or other supervisors are used or
  supported on Linux.
- **SMB mount dependency**: The systemd unit MUST declare `RequiresMountsFor=` for
  every watch volume so the service does not start until the network share(s) are
  mounted. The mount itself MAY be configured via `/etc/fstab` (with `_netdev` and
  `x-systemd.automount`) or a dedicated systemd `.mount` unit.
- **Volume mapping**: Watch directories MUST be SMB shares mounted under
  `/mnt/` (or another documented root); the mount paths MUST be documented in
  `README.md` and `docs/systemd-setup.md`.
- **Privilege scope**: the service runs as the dedicated unprivileged user that
  owns the `--user` unit; any operation requiring elevated privileges (e.g.,
  clock correction) MUST be delegated to an explicitly installed, narrowly scoped
  helper — never run the main process as root.

## Development Workflow & Quality Gates

### Pre-Merge Checklist (every PR)

1. All tests pass: `pytest` — unit, integration, and contract where applicable.
2. TDD cycle verified: the failing test commit predates the implementation commit
   in the PR's commit history.
3. `ruff check .` returns zero violations.
4. `mypy --strict scanner/` returns zero errors.
5. Coverage report shows ≥ 90% on changed/new modules.
6. Documentation updated: README, environment variable table, changelog if needed.
7. PR description includes: motivation, testing notes, and confirmed docs update.

### Branch Strategy

- `main`: always deployable; protected; requires PR + at least one reviewer approval.
- Feature branches: `###-short-description` (e.g., `001-retry-logic`).
- Hotfix branches: `hotfix/short-description`.

### Review Standards

- At least one reviewer approval is required before merge to `main`.
- Reviewer MUST verify documentation is current (Principle V).
- Reviewer MUST verify TDD cycle evidence in the commit history (Principle II).
- Reviewer MUST confirm quality gates passed (Principle III).
- Where the PR adds or changes platform-specific deployment artifacts, the reviewer
  MUST verify both platform sections (macOS and Linux) remain consistent with
  Principle I — neither platform may regress while the other is improved.

## Governance

This constitution supersedes all informal conventions and verbal agreements for
the pms-scanner project.

- Amendments require a dedicated PR to `.specify/memory/constitution.md` with an
  incremented version number, a rationale comment in the PR description, and at
  least one reviewer approval.
- Version policy: MAJOR for principle removals or redefinitions; MINOR for new
  principles or sections; PATCH for clarifications and wording fixes.
- Compliance is reviewed at feature-start (Constitution Check in `plan.md`) and
  again at PR review time.
- All contributors and AI agents MUST read this constitution before beginning any
  work on the project.

**Version**: 3.0.0 | **Ratified**: 2026-04-08 | **Last Amended**: 2026-05-15
