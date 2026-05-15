"""NTP offset measurement, startup gate, and recurring drift monitor (004).

The main process stays unprivileged: it only *measures* offset via
``ntplib``. Clock correction is delegated to an out-of-band privileged
helper (research.md §3). Obviously-wrong responses (kiss-of-death
stratum 16, or an offset magnitude exceeding one day) are rejected.
"""

from __future__ import annotations

import logging
import socket
import time
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


class NTPStartupError(RuntimeError):
    """Startup gate failed — the process must refuse to start (FR-022/024)."""


class _Measurer(Protocol):
    source: str

    def measure(self) -> NTPMeasurement: ...


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


class NTPGate:
    """Startup gate: block until a clean, in-drift measurement (FR-022/024).

    Repeatedly measures until a non-rejected, reachable response arrives.
    If that response's offset exceeds ``max_drift_seconds`` the process
    must refuse to start; if no usable response arrives within
    ``timeout_seconds`` the process must also refuse to start. Either way
    a single ERROR line names the source and (when known) the offset.
    """

    def __init__(
        self,
        client: _Measurer,
        *,
        max_drift_seconds: float,
        timeout_seconds: float,
        poll_interval_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._max_drift = max_drift_seconds
        self._timeout = timeout_seconds
        self._poll = poll_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic

    def verify(self) -> NTPMeasurement:
        deadline = self._monotonic() + self._timeout
        while True:
            measurement: NTPMeasurement | None
            try:
                measurement = self._client.measure()
            except NTPUnreachableError:
                measurement = None

            if measurement is not None and measurement.outcome == "ok":
                if abs(measurement.offset_seconds) > self._max_drift:
                    logger.error(
                        "NTP startup gate FAILED: measured offset %.6fs "
                        "against source %s exceeds max drift %.3fs — "
                        "refusing to start (FR-022)",
                        measurement.offset_seconds,
                        self._client.source,
                        self._max_drift,
                    )
                    raise NTPStartupError(
                        f"clock offset {measurement.offset_seconds:.6f}s vs "
                        f"{self._client.source} exceeds max drift "
                        f"{self._max_drift}s"
                    )
                return measurement

            if self._monotonic() >= deadline:
                logger.error(
                    "NTP startup gate FAILED: no usable response from "
                    "source %s within %.0fs — refusing to start (FR-024)",
                    self._client.source,
                    self._timeout,
                )
                raise NTPStartupError(
                    f"NTP source {self._client.source} unreachable/invalid "
                    f"within {self._timeout}s"
                )
            self._sleep(self._poll)
