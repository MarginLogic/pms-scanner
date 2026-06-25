"""
Shared in-memory state for the fleet (004).

`AppState` is a thin thread→async SSE event bus shared by the dashboard
and `BatchRunner`. `BatchRunState` is the per-(machine, environment)
dashboard container (data-model.md). `make_logger` is an
env/machine-tagging, secret-redacting logger adapter (Constitution IV).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .machine import MachineIdentity
from .ntp import ClockSyncEvent

__all__ = [
    "AppState",
    "BatchRunState",
    "ClockSyncEvent",
    "ErrorRecord",
    "PerEnvRunState",
    "app_state",
    "make_logger",
]


class AppState:
    """Thread→async SSE event bus.

    `loop` is wired in `__main__` before uvicorn serves; `emit_event` is
    safe to call from any worker thread. Events are dropped silently if no
    loop is attached yet (e.g. during unit tests).
    """

    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    def emit_event(self, event: dict[str, Any]) -> None:
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(self.event_queue.put_nowait, event)


# Module-level singleton
app_state = AppState()


# ===========================================================================
# 004 — per-(machine, environment) state container (data-model.md)
# ===========================================================================


@dataclass(slots=True)
class ErrorRecord:
    """One environment-scoped error, surfaced on the dashboard."""

    filename: str
    message: str
    page_num: int | None = None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class PerEnvRunState:
    """Counters + current activity for one environment on this machine."""

    machine: str
    environment: str
    current_file: str | None = None
    current_page: int = 0
    total_pages: int = 0
    files_processed: int = 0
    pages_uploaded: int = 0
    errors: list[ErrorRecord] = field(default_factory=list)
    last_run_started_at: datetime | None = None
    last_run_finished_at: datetime | None = None


_DRIFT_WARNING_OUTCOMES = frozenset(
    {"drift_uncorrected", "unreachable", "rejected_kod"}
)


class BatchRunState:
    """In-memory dashboard state keyed by ``(machine, environment)``.

    A single :class:`threading.RLock` guards every counter mutation so
    concurrent per-env scheduler jobs never lose updates. ``recent_clock_sync``
    / ``last_drift_warning`` are plain attributes (assigned atomically) so this
    object can also serve as a :class:`scanner.ntp._DriftSink`.
    """

    def __init__(
        self, machine: MachineIdentity, env_names: Iterable[str]
    ) -> None:
        self.machine = machine
        self.per_env: dict[str, PerEnvRunState] = {
            name: PerEnvRunState(machine=machine.name, environment=name)
            for name in env_names
        }
        self.recent_clock_sync: ClockSyncEvent | None = None
        self.last_drift_warning: ClockSyncEvent | None = None
        self._lock = threading.RLock()

    def env(self, name: str) -> PerEnvRunState:
        """Return the per-env state, raising ``KeyError`` if unconfigured."""
        return self.per_env[name]

    def add_pages_uploaded(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.per_env[name].pages_uploaded += n

    def add_files_processed(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.per_env[name].files_processed += n

    def add_error(self, name: str, record: ErrorRecord) -> None:
        with self._lock:
            self.per_env[name].errors.append(record)

    def reset_daily(self) -> None:
        """Zero every env's day-scoped counters (files/pages/errors).

        Called once a day by the scheduler's midnight job so the dashboard
        reads as a per-day tally rather than a since-start running total.
        In-flight activity (``current_*``) and the ``last_run_*`` timestamps
        are left untouched — they describe live/most-recent work, not a daily
        count. Guarded by the same ``RLock`` as the ``add_*`` mutators so it
        can't race a scheduler job recording work at the rollover.
        """
        with self._lock:
            for st in self.per_env.values():
                st.files_processed = 0
                st.pages_uploaded = 0
                st.errors.clear()

    def set_current(
        self,
        name: str,
        *,
        current_file: str | None = None,
        current_page: int | None = None,
        total_pages: int | None = None,
    ) -> None:
        with self._lock:
            st = self.per_env[name]
            if current_file is not None:
                st.current_file = current_file
            if current_page is not None:
                st.current_page = current_page
            if total_pages is not None:
                st.total_pages = total_pages

    def mark_run_started(self, name: str, when: datetime) -> None:
        with self._lock:
            self.per_env[name].last_run_started_at = when

    def mark_run_finished(self, name: str, when: datetime) -> None:
        with self._lock:
            self.per_env[name].last_run_finished_at = when

    def record_clock_sync(self, event: ClockSyncEvent) -> None:
        with self._lock:
            self.recent_clock_sync = event
            if event.outcome in _DRIFT_WARNING_OUTCOMES:
                self.last_drift_warning = event


# ===========================================================================
# 004 — env/machine-tagging, secret-redacting logger (Constitution IV)
# ===========================================================================


class _RedactingFilter(logging.Filter):
    """Masks registered secret substrings in the final formatted message."""

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._patterns = [
            re.compile(re.escape(s)) for s in secrets if s and s.strip()
        ]

    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        # A redactor transforms and passes records through — it never
        # drops one. Returning the (possibly mutated) record satisfies
        # the logging contract (any truthy result keeps the record; a
        # LogRecord is used as-is on 3.12+) without an invariant
        # constant return.
        if self._patterns:
            message = record.getMessage()
            for pat in self._patterns:
                message = pat.sub("***", message)
            record.msg = message
            record.args = None
        return record


class _EnvMachineAdapter(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Prefixes every message with ``[machine=… env=…]``."""

    def process(
        self, msg: str, kwargs: Any
    ) -> tuple[str, Any]:
        extra = self.extra or {}
        machine = extra.get("machine", "?")
        env = extra.get("env", "?")
        return f"[machine={machine} env={env}] {msg}", kwargs


def make_logger(
    name: str,
    *,
    machine: str,
    env: str,
    secrets: Iterable[str] = (),
) -> logging.LoggerAdapter:  # type: ignore[type-arg]
    """Build an env/machine-tagged adapter with secret redaction.

    Every emitted record is prefixed with ``[machine=… env=…]`` and any
    registered secret value (e.g. an API token) is masked to ``***``.
    """
    base = logging.getLogger(name)
    secret_list = [s for s in secrets if s and s.strip()]
    if secret_list and not any(
        isinstance(f, _RedactingFilter) for f in base.filters
    ):
        base.addFilter(_RedactingFilter(secret_list))
    return _EnvMachineAdapter(base, {"machine": machine, "env": env})
