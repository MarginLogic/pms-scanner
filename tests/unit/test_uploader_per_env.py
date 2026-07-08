"""Unit tests for the env-aware uploader — T019 (004 US1).

upload_page(env, ...) MUST post to ``env.backend_base_url +
/api/scanned-images/upload`` with ``X-API-Key: <env token>``
and the env's optional requisition_id — never a hard-coded host or
module-level config (FR-002/003/005, upload-endpoint.md contract).

The backend authenticates opaque ``pms_…`` API keys ONLY via the
``X-API-Key`` header; ``Authorization: Bearer`` routes to JWT decode
and always 401s for an API key. (Regression fixed post-T020.)
"""
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from config import Environment
from PIL import Image
from pydantic import SecretStr
from uploader import UploadOutcome, upload_page


@pytest.fixture()
def image() -> Image.Image:
    return Image.new("RGB", (60, 60), color=(10, 20, 30))


def _env(
    name: str, url: str, token: str, *, req_id=None, tmp_path: Path
) -> Environment:
    return Environment(
        name=name,  # type: ignore[arg-type]
        watch_dir=tmp_path / name,
        backend_base_url=url,
        api_token=SecretStr(token),
        requisition_id=req_id,
        schedule_offset_seconds=0,
    )


def _resp(status: int, body: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    if status >= 400:
        from requests import HTTPError

        r.raise_for_status.side_effect = HTTPError(response=r)
    else:
        r.raise_for_status.return_value = None
    return r


def _ok() -> MagicMock:
    return _resp(
        200, {"batch_id": "b1", "images": [{"original_file_name": "f"}], "rejected": []}
    )


PROD = ("production", "https://adg.mpsinc.io", "prod-token-XYZ")
STG = ("staging", "https://dev.adg.mpsinc.io", "stg-token-ABC")


@pytest.mark.parametrize("name,url,token", [PROD, STG])
def test_posts_to_env_url_with_api_key(
    name: str, url: str, token: str, image: Image.Image, tmp_path: Path
) -> None:
    env = _env(name, url, token, tmp_path=tmp_path)
    with patch("uploader.requests.post", return_value=_ok()) as post:
        ok = upload_page(env, tmp_path / "scan.pdf", 1, 3, image)
    assert ok is UploadOutcome.ACCEPTED
    called_url = post.call_args.args[0] if post.call_args.args else post.call_args.kwargs["url"]
    assert called_url == f"{url}/api/scanned-images/upload"
    headers = post.call_args.kwargs["headers"]
    assert headers["X-API-Key"] == token
    assert "Authorization" not in headers


def test_no_hardcoded_host_anywhere(image: Image.Image, tmp_path: Path) -> None:
    env = _env(
        "staging", "https://sentinel.example.invalid", "tok", tmp_path=tmp_path
    )
    with patch("uploader.requests.post", return_value=_ok()) as post:
        upload_page(env, tmp_path / "x.pdf", 2, 2, image)
    url = post.call_args.args[0] if post.call_args.args else post.call_args.kwargs["url"]
    assert url.startswith("https://sentinel.example.invalid")


def test_filename_convention_and_page_number(
    image: Image.Image, tmp_path: Path
) -> None:
    env = _env(*PROD, tmp_path=tmp_path)
    with patch("uploader.requests.post", return_value=_ok()) as post:
        upload_page(env, tmp_path / "scan_2026.pdf", 7, 33, image)
    files = post.call_args.kwargs["files"]
    sent_name = files[0][1][0]
    assert sent_name == "scan_2026_p007.tiff"


def test_requisition_id_sent_when_present(
    image: Image.Image, tmp_path: Path
) -> None:
    rid = uuid4()
    env = _env(
        "production", "https://adg.mpsinc.io", "t", req_id=rid, tmp_path=tmp_path
    )
    with patch("uploader.requests.post", return_value=_ok()) as post:
        upload_page(env, tmp_path / "x.pdf", 1, 1, image)
    data = post.call_args.kwargs["data"]
    assert data["requisition_id"] == str(rid)


def test_no_requisition_id_field_when_absent(
    image: Image.Image, tmp_path: Path
) -> None:
    env = _env(*PROD, tmp_path=tmp_path)
    with patch("uploader.requests.post", return_value=_ok()) as post:
        upload_page(env, tmp_path / "x.pdf", 1, 1, image)
    assert "requisition_id" not in post.call_args.kwargs["data"]


def test_4xx_returns_false_no_retry(image: Image.Image, tmp_path: Path) -> None:
    env = _env(*PROD, tmp_path=tmp_path)
    with patch(
        "uploader.requests.post", return_value=_resp(403, {})
    ) as post:
        ok = upload_page(env, tmp_path / "x.pdf", 1, 1, image)
    assert ok is UploadOutcome.REJECTED
    assert post.call_count == 1


def test_5xx_retried_then_succeeds(image: Image.Image, tmp_path: Path) -> None:
    env = _env(*STG, tmp_path=tmp_path)
    seq = [_resp(503, {}), _ok()]
    with patch("uploader.requests.post", side_effect=seq):
        with patch("uploader.time.sleep"):
            ok = upload_page(
                env, tmp_path / "x.pdf", 1, 1, image, max_retries=3
            )
    assert ok is UploadOutcome.ACCEPTED


def test_token_never_appears_in_logs(
    image: Image.Image, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    env = _env(
        "production", "https://adg.mpsinc.io", "super-secret-tok", tmp_path=tmp_path
    )
    import logging

    with caplog.at_level(logging.DEBUG):
        with patch("uploader.requests.post", return_value=_ok()):
            upload_page(env, tmp_path / "x.pdf", 1, 1, image)
    assert all("super-secret-tok" not in r.getMessage() for r in caplog.records)


# --- coverage: rate limiter, rejected items, network exhaustion ---


def test_rate_limiter_sleeps_when_window_full() -> None:
    import uploader as up

    slept: list[float] = []
    with up._rate_lock:
        up._rate_history.clear()
        now = __import__("time").monotonic()
        for _ in range(up._RATE_LIMIT_MAX):
            up._rate_history.append(now)
    with patch("uploader.time.sleep", side_effect=slept.append):
        with patch(
            "uploader.time.monotonic",
            side_effect=[now, now, now + up._RATE_LIMIT_WINDOW + 1],
        ):
            up._rate_limit_acquire()
    assert slept and slept[0] > 0
    up._rate_history.clear()


def test_rejected_items_logged_and_returns_false(
    image: Image.Image, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    env = _env(*PROD, tmp_path=tmp_path)
    resp = _resp(
        200,
        {"batch_id": "b", "images": [], "rejected": [
            {"file_name": "x_p001.tiff", "reason": "duplicate"}]},
    )
    with caplog.at_level(logging.WARNING, logger="scanner.uploader"):
        with patch("uploader.requests.post", return_value=resp):
            ok = upload_page(env, tmp_path / "x.pdf", 1, 1, image)
    assert ok is UploadOutcome.REJECTED
    assert any("rejected" in r.getMessage().lower() for r in caplog.records)


def test_network_exception_retries_then_exhausts(
    image: Image.Image, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    import requests as rq

    env = _env(*STG, tmp_path=tmp_path)
    with caplog.at_level(logging.ERROR, logger="scanner.uploader"):
        with patch(
            "uploader.requests.post",
            side_effect=rq.ConnectionError("net down"),
        ):
            with patch("uploader.time.sleep"):
                ok = upload_page(
                    env, tmp_path / "x.pdf", 1, 1, image, max_retries=3
                )
    assert ok is UploadOutcome.FAILED
    assert any("Exhausted" in r.getMessage() for r in caplog.records)
