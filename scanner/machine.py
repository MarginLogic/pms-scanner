"""Machine identity: the self-declared name of the running host (004).

The machine name names this host's ``in-progress/<name>/`` subfolder under
every environment and tags logs / dashboard records. Validation runs at
startup; an invalid name means the process refuses to start (FR-015).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
_RESERVED = frozenset({"in-progress", "processed", "..", "."})


class InvalidMachineIdentityError(ValueError):
    """Raised when the configured machine identity is missing or illegal."""


class _WatchEnv(Protocol):
    """Anything that can resolve a per-machine in-progress directory."""

    def in_progress_dir(self, machine: MachineIdentity) -> Path: ...


@dataclass(frozen=True, slots=True)
class MachineIdentity:
    """A validated, normalized host name (e.g. ``macmini``, ``nuc``)."""

    name: str

    def __post_init__(self) -> None:
        raw = self.name
        stripped = raw.strip() if isinstance(raw, str) else ""
        if not stripped:
            raise InvalidMachineIdentityError(
                "MACHINE_IDENTITY is missing or blank"
            )
        if stripped in _RESERVED:
            raise InvalidMachineIdentityError(
                f"MACHINE_IDENTITY '{stripped}' is a reserved name "
                f"(one of {sorted(_RESERVED)})"
            )
        if not _NAME_RE.match(stripped):
            raise InvalidMachineIdentityError(
                f"MACHINE_IDENTITY '{stripped}' must match "
                r"^[a-z0-9][a-z0-9_-]{0,30}$"
            )
        object.__setattr__(self, "name", stripped)

    def in_progress_dir(self, env: _WatchEnv) -> Path:
        """Resolve this machine's in-progress subfolder for ``env``."""
        return env.in_progress_dir(self)
