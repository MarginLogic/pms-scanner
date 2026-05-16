"""Dual-environment routing integration tests — T025 (US1).

test_production_only_routing: both envs configured, production backend
mocked (asserts calls), staging backend a sentinel that FAILS the test
if hit. A 3-page PDF dropped in the production folder + POST
/run?environment=production must yield exactly 3 uploads to production,
zero to staging, the file in <prod>/processed/, and an empty
in-progress/<machine>/ (SC-001/SC-002).
"""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import fitz
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

PROD_BASE = "https://adg.mpsinc.io"
STAGING_BASE = "https://dev.adg.mpsinc.io"


def _make_pdf(path: Path, pages: int) -> None:
    doc = fitz.open()
    for _ in range(pages):
        doc.new_page(width=612, height=792)
    doc.save(str(path))
    doc.close()


def _settings(tmp_path: Path):
    prod = tmp_path / "prod"
    stg = tmp_path / "stg"
    prod.mkdir()
    stg.mkdir()
    env = {
        "MACHINE_IDENTITY": "macmini",
        "NTP__STARTUP_REQUIRED": "false",
        "FILE_SETTLE_SECONDS": "0",
        "ENVIRONMENTS": "production,staging",
        "ENV_PRODUCTION__WATCH_DIR": str(prod),
        "ENV_PRODUCTION__BACKEND_BASE_URL": PROD_BASE,
        "ENV_PRODUCTION__API_TOKEN": "prod-token",
        "ENV_PRODUCTION__SCHEDULE_OFFSET_SECONDS": "0",
        "ENV_STAGING__WATCH_DIR": str(stg),
        "ENV_STAGING__BACKEND_BASE_URL": STAGING_BASE,
        "ENV_STAGING__API_TOKEN": "stg-token",
        "ENV_STAGING__SCHEDULE_OFFSET_SECONDS": "15",
    }
    from config import load_settings

    with patch.dict(os.environ, env, clear=True):
        return load_settings(dotenv=False)


def _routing_post(prod_calls: list[str], staging_hit: list[bool]):
    def fake_post(url, *a, **kw):
        if url.startswith(STAGING_BASE):
            staging_hit.append(True)
            raise AssertionError(f"staging backend was hit: {url}")
        prod_calls.append(url)
        r = MagicMock()
        r.status_code = 200
        r.raise_for_status.return_value = None
        r.json.return_value = {
            "batch_id": "b",
            "images": [{"original_file_name": "f"}],
            "rejected": [],
        }
        return r

    return fake_post


@pytest_asyncio.fixture()
async def configured(tmp_path: Path):
    import dashboard
    from state import BatchRunState

    settings = _settings(tmp_path)
    state = BatchRunState(settings.machine, [e.name for e in settings.environments])
    dashboard.configure(settings, state)
    async with AsyncClient(
        transport=ASGITransport(app=dashboard.app), base_url="http://test"
    ) as ac:
        yield ac, settings, state


@pytest.mark.asyncio
async def test_production_only_routing(configured) -> None:
    client, settings, state = configured
    prod_env = next(e for e in settings.environments if e.name == "production")
    _make_pdf(prod_env.watch_dir / "scan.pdf", pages=3)

    prod_calls: list[str] = []
    staging_hit: list[bool] = []
    with patch(
        "uploader.requests.post",
        side_effect=_routing_post(prod_calls, staging_hit),
    ):
        resp = await client.post("/run?environment=production")

    assert resp.status_code == 202
    body = resp.json()
    assert body["machine"] == "macmini"
    assert body["triggered"] == ["production"]
    assert "production" in body["run_ids"]

    assert len(prod_calls) == 3
    assert all(c.startswith(PROD_BASE) for c in prod_calls)
    assert staging_hit == []

    assert (prod_env.processed_dir / "scan.pdf").is_file()
    in_self = prod_env.in_progress_dir(settings.machine)
    assert list(in_self.iterdir()) == []
    assert state.env("production").pages_uploaded == 3
    assert state.env("production").files_processed == 1


@pytest.mark.asyncio
async def test_unknown_environment_returns_404(configured) -> None:
    client, _settings, _state = configured
    resp = await client.post("/run?environment=qa")
    assert resp.status_code == 404
    assert "qa" in resp.json()["detail"]


def _recording_post(by_base: dict[str, list[str]]):
    def fake_post(url, *a, **kw):
        base = PROD_BASE if url.startswith(PROD_BASE) else STAGING_BASE
        by_base.setdefault(base, []).append(url)
        r = MagicMock()
        r.status_code = 200
        r.raise_for_status.return_value = None
        r.json.return_value = {
            "batch_id": "b",
            "images": [{"original_file_name": "f"}],
            "rejected": [],
        }
        return r

    return fake_post


@pytest.mark.asyncio
async def test_staging_only_routing(configured) -> None:
    """Mirror of production-only routing for staging (T026, SC-001/002)."""
    client, settings, state = configured
    stg = next(e for e in settings.environments if e.name == "staging")
    prod = next(e for e in settings.environments if e.name == "production")
    _make_pdf(stg.watch_dir / "qa.pdf", pages=2)

    def routing(url, *a, **kw):
        if url.startswith(PROD_BASE):
            raise AssertionError(f"production backend was hit: {url}")
        r = MagicMock()
        r.status_code = 200
        r.raise_for_status.return_value = None
        r.json.return_value = {
            "batch_id": "b",
            "images": [{"original_file_name": "f"}],
            "rejected": [],
        }
        return r

    with patch("uploader.requests.post", side_effect=routing):
        resp = await client.post("/run?environment=staging")

    assert resp.status_code == 202
    assert resp.json()["triggered"] == ["staging"]
    assert (stg.processed_dir / "qa.pdf").is_file()
    assert state.env("staging").pages_uploaded == 2
    # Production untouched.
    assert state.env("production").pages_uploaded == 0
    prod_in = prod.in_progress_dir(settings.machine)
    assert not prod_in.exists() or list(prod_in.iterdir()) == []


@pytest.mark.asyncio
async def test_cross_routing_impossible(configured) -> None:
    """One PDF in each folder; trigger both; zero exchanges (T027, SC-002)."""
    client, settings, state = configured
    prod = next(e for e in settings.environments if e.name == "production")
    stg = next(e for e in settings.environments if e.name == "staging")
    _make_pdf(prod.watch_dir / "p.pdf", pages=2)
    _make_pdf(stg.watch_dir / "s.pdf", pages=3)

    by_base: dict[str, list[str]] = {}
    with patch("uploader.requests.post", side_effect=_recording_post(by_base)):
        r1 = await client.post("/run?environment=production")
        r2 = await client.post("/run?environment=staging")

    assert r1.status_code == 202 and r2.status_code == 202
    # Every production page hit only adg.mpsinc.io; every staging page only dev.
    assert len(by_base[PROD_BASE]) == 2
    assert len(by_base[STAGING_BASE]) == 3
    assert all(u.startswith(PROD_BASE) for u in by_base[PROD_BASE])
    assert all(u.startswith(STAGING_BASE) for u in by_base[STAGING_BASE])
    assert (prod.processed_dir / "p.pdf").is_file()
    assert (stg.processed_dir / "s.pdf").is_file()
    assert state.env("production").pages_uploaded == 2
    assert state.env("staging").pages_uploaded == 3


@pytest.mark.asyncio
async def test_simultaneous_dual_env_trigger(configured) -> None:
    """POST /run (no env) fans out all enabled envs concurrently (T029).

    Both runs must overlap in wall-clock time and each file ends in its
    own env's processed/ (US2 acceptance scenario 2; SC-008).
    """
    import threading
    import time as _t

    client, settings, state = configured
    prod = next(e for e in settings.environments if e.name == "production")
    stg = next(e for e in settings.environments if e.name == "staging")
    _make_pdf(prod.watch_dir / "p.pdf", pages=2)
    _make_pdf(stg.watch_dir / "s.pdf", pages=2)

    lock = threading.Lock()
    intervals: dict[str, list[float]] = {PROD_BASE: [], STAGING_BASE: []}

    def slow_post(url, *a, **kw):
        base = PROD_BASE if url.startswith(PROD_BASE) else STAGING_BASE
        with lock:
            intervals[base].append(_t.monotonic())
        _t.sleep(0.05)
        with lock:
            intervals[base].append(_t.monotonic())
        r = MagicMock()
        r.status_code = 200
        r.raise_for_status.return_value = None
        r.json.return_value = {
            "batch_id": "b",
            "images": [{"original_file_name": "f"}],
            "rejected": [],
        }
        return r

    with patch("uploader.requests.post", side_effect=slow_post):
        resp = await client.post("/run")

    assert resp.status_code == 202
    body = resp.json()
    assert body["machine"] == "macmini"
    assert set(body["triggered"]) == {"production", "staging"}
    assert set(body["run_ids"]) == {"production", "staging"}

    # Overlap: production's [first_enter, last_exit] intersects staging's.
    p_start, p_end = intervals[PROD_BASE][0], intervals[PROD_BASE][-1]
    s_start, s_end = intervals[STAGING_BASE][0], intervals[STAGING_BASE][-1]
    assert p_start < s_end and s_start < p_end  # intervals overlap

    assert (prod.processed_dir / "p.pdf").is_file()
    assert (stg.processed_dir / "s.pdf").is_file()
