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
