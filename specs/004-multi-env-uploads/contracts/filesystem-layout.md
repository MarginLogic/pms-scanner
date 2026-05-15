# Contract: Filesystem Layout

**Branch**: `004-multi-env-uploads` | **Date**: 2026-05-15

Defines the on-disk shape of each environment's watch tree and the rules each
fleet machine must follow when reading or writing to it. Source of truth for
`scanner/batch.py` and `scanner/machine.py`.

---

## Per-environment tree

```text
<ENV_<NAME>__WATCH_DIR>/                ← watch directory (operator drops PDFs here)
├── *.pdf
├── in-progress/                         ← created by this app on first run
│   ├── macmini/                         ← created by macmini on its first claim
│   │   └── <claimed-by-macmini>.pdf
│   ├── nuc/                             ← created by nuc on its first claim
│   │   └── <claimed-by-nuc>.pdf
│   └── <future-machine>/                ← any future fleet host
└── processed/                           ← terminal state, shared across the fleet
    └── *.pdf
```

---

## Ownership and access rules

| Path | Read by | Write by | Notes |
|---|---|---|---|
| `<watch>/*.pdf` | Every machine (during scan) | Operator (drops files); each machine (atomic rename out into its own `in-progress/<self>/`) | Source of truth for "new work." |
| `<watch>/in-progress/` | Every machine (enumeration to learn what subfolders exist is **forbidden** for correctness; only `in-progress/<self>/` is consulted) | Every machine (each `mkdir`s its own subfolder at startup; never another machine's) | Do **not** list this directory and act on its contents — it is purely a namespace container. |
| `<watch>/in-progress/<self>/` | This machine only | This machine only | All claims and crash-recovery scoped here. |
| `<watch>/in-progress/<peer>/` | **Forbidden** (read or write) | **Forbidden** | Peer subfolders are off-limits. Even merely *listing* a peer subfolder is forbidden in code paths that act on the result. |
| `<watch>/processed/` | Operator, every machine | Every machine | Terminal; no claim, no race. Files moved here are done. |

Permissions (Linux): the `in-progress/<self>/` directory is created with mode
`0700` so other UIDs cannot accidentally interfere. On macOS the default umask
applies; SMB ACLs are operator-managed.

---

## Claim semantics (FR-017)

A file move from `<watch>/<file>.pdf` into `<watch>/in-progress/<self>/<file>.pdf`
is an **atomic rename** (`os.rename`, which delegates to `rename(2)` →
`FileRenameInformation` on SMB).

- Exactly one machine's `os.rename` call observes success for any given source
  path; every other machine observing the same source name receives
  `FileNotFoundError` and treats the file as already claimed by a peer — no
  error, log at `DEBUG`, continue.
- The destination `in-progress/<self>/` MUST exist before the rename. Each
  machine creates it once at startup (`mkdir -p`-equivalent via Python).
- No lock files, no `.partial` suffixes, no advisory locks (`flock` is
  unreliable on SMB; rejected in research.md §1).

## Crash-recovery semantics (FR-008)

At startup, **before** the first scheduled poll fires:

1. Resolve `in_progress_dir(self) = <watch>/in-progress/<self-machine-name>/`.
2. `os.listdir` that directory only.
3. For each entry, `os.rename` it back to `<watch>/<filename>`. If a file with
   that name already exists at the destination (operator created a duplicate
   while we were down), append a timestamp suffix to disambiguate and `WARN`.
4. Do **not** read, list, stat, or `os.rename` anything from any peer
   subfolder.

Operators repairing a stuck state may manually move files between any folders;
that is acceptable behavior (see spec edge case "operator manually moves a
file out of another machine's `in-progress/<machine>/` subfolder").

---

## Naming validation

`MachineIdentity.name` is validated against `^[a-z0-9][a-z0-9_-]{0,30}$` and a
reserved-name blocklist (`in-progress`, `processed`, `..`, `.`). This prevents:

- `in-progress/in-progress/` collisions
- `in-progress/processed/` collisions (would hide files from the processed dir
  walker on the operator's side)
- Path-traversal characters
- Names that won't round-trip on case-insensitive SMB (`processed` ≡ `Processed`)
