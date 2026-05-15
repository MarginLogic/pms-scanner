# Specification Quality Checklist: Dual-Environment Upload Routing (Staging & Production)

**Purpose**: Validate specification completeness and quality before proceeding to planning
**Created**: 2026-05-15
**Feature**: [spec.md](../spec.md)

## Content Quality

- [x] No implementation details (languages, frameworks, APIs)
- [x] Focused on user value and business needs
- [x] Written for non-technical stakeholders
- [x] All mandatory sections completed

## Requirement Completeness

- [x] No [NEEDS CLARIFICATION] markers remain
- [x] Requirements are testable and unambiguous
- [x] Success criteria are measurable
- [x] Success criteria are technology-agnostic (no implementation details)
- [x] All acceptance scenarios are defined
- [x] Edge cases are identified
- [x] Scope is clearly bounded
- [x] Dependencies and assumptions identified

## Feature Readiness

- [x] All functional requirements have clear acceptance criteria
- [x] User scenarios cover primary flows
- [x] Feature meets measurable outcomes defined in Success Criteria
- [x] No implementation details leak into specification

## Notes

- Items marked incomplete require spec updates before `/speckit.clarify` or `/speckit.plan`
- Backend hostnames (`adg.mpsinc.io`, `dev.adg.mpsinc.io`) were retained verbatim because they are the user-stated destinations — they describe *what* environment a file should land in, not *how* the upload is implemented, and so are treated as business-level routing rules rather than implementation detail.
- The `POST /api/scanned-images/upload` reference in Assumptions refers to the existing contract from feature 003, included only to scope the assumption that no new protocol work is required for this feature.
- The 2026-05-15 update added staggered per-environment scheduling (production `:00`, staging `:15`) and concurrent execution. "Concurrent" is described as user-visible behavior (one environment does not block the other) rather than as a specific threading mechanism — the choice between threads, processes, or async workers is deferred to planning.
- A second 2026-05-15 update added a multi-machine deployment model: each host self-identifies (`macmini`, `nuc`) and claims files into its own `in-progress/<machine>/` subfolder. Default fleet schedule is `macmini` production `:00` / staging `:15`, `nuc` production `:30` / staging `:45`. The atomic-claim requirement (FR-017) is stated as a contract — "exactly one machine wins" — without prescribing the underlying filesystem primitive, since the appropriate mechanism (POSIX `rename`, lock file, advisory lock, etc.) depends on the share type and is decided in planning.
- A third 2026-05-15 update added NTP-based clock synchronization (FR-020–FR-024) so the fleet stagger cannot drift apart. NTP is named because the user requested it and because it is the protocol that semantically matches the requirement (network-based time sync); the spec does not prescribe whether sync is done via an OS time-sync service, a privileged helper, or in-process — that is a planning decision. The 1-second default maximum drift is well inside the 15-second stride between adjacent offsets, leaving margin for the next sync cycle.
