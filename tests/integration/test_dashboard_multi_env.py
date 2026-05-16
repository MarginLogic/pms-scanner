"""Multi-env dashboard contract — T045 (/status) + T047 (SSE) (US5).

/status must match contracts/dashboard-events.md: top-level ``machine``,
an ``ntp`` block, and ``environments`` keyed by name with per-env
``current_run``/``last_run``. Every SSE event carries ``env`` +
``machine``; a ``clock_sync`` event is emitted after an NTP cycle.
"""
import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


def _settings(tmp_path: Path):
    p = tmp_path / "p"
    s = tmp_path / "s"
    p.mkdir()
    s.mkdir()
    env = {
        "MACHINE_IDENTITY": "macmini",
        "NTP__STARTUP_REQUIRED": "false",
        "ENVIRONMENTS": "production,staging",
        "ENV_PRODUCTION__WATCH_DIR": str(p),
        "ENV_PRODUCTION__BACKEND_BASE_URL": "https://adg.mpsinc.io",
        "ENV_PRODUCTION__API_TOKEN": "pt",
        "ENV_PRODUCTION__SCHEDULE_OFFSET_SECONDS": "0",
        "ENV_STAGING__WATCH_DIR": str(s),
        "ENV_STAGING__BACKEND_BASE_URL": "https://dev.adg.mpsinc.io",
        "ENV_STAGING__API_TOKEN": "st",
        "ENV_STAGING__SCHEDULE_OFFSET_SECONDS": "15",
    }
    from config import load_settings

    with patch.dict(os.environ, env, clear=True):
        return load_settings(dotenv=False)


@pytest_asyncio.fixture()
async def configured(tmp_path: Path):
    import dashboard
    from state import BatchRunState

    settings = _settings(tmp_path)
    state = BatchRunState(settings.machine, [e.name for e in settings.environments])
    dashboard.configure(settings, state)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=dashboard.app), base_url="http://test"
        ) as ac:
            yield ac, settings, state, dashboard
    finally:
        dashboard._settings = None
        dashboard._run_state = None


@pytest.mark.asyncio
async def test_status_shape(configured) -> None:
    client, settings, state, _dash = configured
    from ntp import ClockSyncEvent

    state.record_clock_sync(
        ClockSyncEvent(datetime.now(UTC), "pool.ntp.org", 0.043, "ok")
    )
    state.add_pages_uploaded("production", 2)
    state.set_current("production", current_file="scan.pdf", current_page=2,
                      total_pages=3)

    body = (await client.get("/status")).json()

    assert body["machine"] == "macmini"
    ntp = body["ntp"]
    assert ntp["source"] == "pool.ntp.org"
    assert ntp["offset_seconds"] == pytest.approx(0.043)
    assert ntp["outcome"] == "ok"
    assert "last_drift_warning" in ntp

    envs = body["environments"]
    assert set(envs) == {"production", "staging"}
    prod = envs["production"]
    assert prod["enabled"] is True
    assert prod["schedule_offset_seconds"] == 0
    assert prod["backend_base_url"] == "https://adg.mpsinc.io"
    assert "current_run" in prod and "last_run" in prod
    assert envs["staging"]["backend_base_url"] == "https://dev.adg.mpsinc.io"
    assert envs["staging"]["schedule_offset_seconds"] == 15


@pytest.mark.asyncio
async def test_sse_events_tagged(configured) -> None:
    client, settings, state, dash = configured

    async def read_events(n: int) -> list[dict]:
        events: list[dict] = []
        async with client.stream("GET", "/events") as resp:
            data_lines: list[str] = []
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "" and data_lines:
                    payload = data_lines[-1]
                    data_lines = []
                    try:
                        ev = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if ev:
                        events.append(ev)
                        if len(events) >= n:
                            return events
        return events

    task = asyncio.create_task(read_events(4))
    await asyncio.sleep(0.2)

    # Synthetic per-page events (as a BatchRunner would emit them).
    for et in ("run_started", "page_done", "file_done", "run_done"):
        dash._app_state.emit_event(
            {"type": et, "env": "production", "machine": "macmini"}
        )
    # One NTP cycle → clock_sync event.
    dash.emit_clock_event(
        {
            "type": "clock_sync",
            "machine": "macmini",
            "source": "pool.ntp.org",
            "offset_seconds": 0.01,
            "outcome": "ok",
        }
    )

    events = await asyncio.wait_for(task, timeout=5)
    tagged = {
        e["type"]: e
        for e in events
        if e.get("type")
        in {"run_started", "page_done", "file_done", "run_done"}
    }
    for et, ev in tagged.items():
        assert ev["env"] == "production", et
        assert ev["machine"] == "macmini", et
    assert any(e.get("type") == "clock_sync" for e in events)
