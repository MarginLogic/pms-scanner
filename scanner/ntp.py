"""NTP offset measurement, startup gate, and recurring drift monitor (004).

The main process stays unprivileged: it only *measures* offset via
``ntplib``. Clock correction is delegated to an out-of-band privileged
helper (research.md §3). Obviously-wrong responses (kiss-of-death
stratum 16, or an offset magnitude exceeding one day) are rejected.
"""

from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

import ntplib

logger = logging.getLogger(__name__)

# Reject any response whose offset magnitude exceeds one day.
_MAX_PLAUSIBLE_OFFSET_SECONDS = 86_400.0
_KISS_OF_DEATH_STRATUM = 16

NTPOutcome = Literal["ok", "rejected_kod"]


class NTPUnreachableError(RuntimeError):
    """The NTP source could not be reached / returned no usable response."""


@dataclass(frozen=True, slots=True)
class NTPMeasurement:
    """One offset measurement against the configured NTP source."""

    source: str
    offset_seconds: float
    measured_at: datetime
    outcome: NTPOutcome
    stratum: int | None = None


class _Stats(Protocol):
    offset: float
    stratum: int


Requester = Callable[[str, int, float], "_Stats"]


def _default_requester(host: str, version: int, timeout: float) -> _Stats:
    stats: _Stats = ntplib.NTPClient().request(
        host, version=version, timeout=timeout
    )
    return stats


class NTPClient:
    """Queries an NTP source and classifies the response."""

    def __init__(
        self,
        source: str,
        *,
        timeout: float = 5.0,
        version: int = 3,
        requester: Requester | None = None,
    ) -> None:
        self._source = source
        self._timeout = timeout
        self._version = version
        self._requester = requester or _default_requester

    @property
    def source(self) -> str:
        return self._source

    def measure(self) -> NTPMeasurement:
        """Query the source once and return a classified measurement.

        Raises :class:`NTPUnreachableError` on any network-level failure.
        """
        try:
            stats = self._requester(self._source, self._version, self._timeout)
        except (
            TimeoutError,
            socket.gaierror,
            ntplib.NTPException,
            OSError,
        ) as exc:
            raise NTPUnreachableError(
                f"NTP source {self._source!r} unreachable: {exc}"
            ) from exc

        offset = float(stats.offset)
        stratum = int(getattr(stats, "stratum", 0))
        measured_at = datetime.now(UTC)

        if (
            stratum >= _KISS_OF_DEATH_STRATUM
            or abs(offset) > _MAX_PLAUSIBLE_OFFSET_SECONDS
        ):
            return NTPMeasurement(
                self._source, offset, measured_at, "rejected_kod", stratum
            )
        return NTPMeasurement(self._source, offset, measured_at, "ok", stratum)
