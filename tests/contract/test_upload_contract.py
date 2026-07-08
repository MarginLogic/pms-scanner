"""
Contract test for POST /api/scanned-images/upload — T015.

Skipped when BACKEND_BASE_URL is not set in the environment.
Requires a live backend reachable at BACKEND_BASE_URL with a valid API_TOKEN.
"""
import io
import os

import pytest
import requests
from PIL import Image


@pytest.mark.skipif(
    not os.environ.get("BACKEND_BASE_URL"),
    reason="BACKEND_BASE_URL not set — skipping live contract test",
)
def test_upload_endpoint_contract():
    """
    POST /api/scanned-images/upload returns the expected JSON shape.

    Response must contain:
        batch_id   str
        images     list
        rejected   list
    """
    backend_url = os.environ["BACKEND_BASE_URL"]
    api_token = os.environ.get("API_TOKEN", "")

    # Build a minimal JPEG in memory
    img = Image.new("RGB", (50, 50), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    buf.seek(0)

    response = requests.post(
        f"{backend_url}/api/scanned-images/upload",
        headers={"X-API-Key": api_token},
        files=[("files", ("contract_test_p001.jpg", buf.read(), "image/jpeg"))],
        timeout=30,
    )

    assert response.status_code == 200, (
        f"Expected 200, got {response.status_code}: {response.text}"
    )

    body = response.json()
    assert "batch_id" in body, f"Missing 'batch_id' in response: {body}"
    assert "images" in body, f"Missing 'images' in response: {body}"
    assert "rejected" in body, f"Missing 'rejected' in response: {body}"
    assert isinstance(body["images"], list)
    assert isinstance(body["rejected"], list)


# ---------------------------------------------------------------------------
# T023 — client-side request shape is identical across both environments
# (upload-endpoint.md: only base URL + X-API-Key value differ per env).
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

from config import Environment  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from uploader import UploadOutcome, upload_page  # noqa: E402

_ENVS = [
    ("production", "https://adg.mpsinc.io", "prod-token"),
    ("staging", "https://dev.adg.mpsinc.io", "staging-token"),
]


def _env(name: str, url: str, token: str, *, tmp_path: Path) -> Environment:
    return Environment(
        name=name,  # type: ignore[arg-type]
        watch_dir=tmp_path / name,
        backend_base_url=url,
        api_token=SecretStr(token),
        schedule_offset_seconds=0,
    )


def _ok() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.raise_for_status.return_value = None
    r.json.return_value = {
        "batch_id": "b",
        "images": [{"original_file_name": "f"}],
        "rejected": [],
    }
    return r


@pytest.mark.parametrize("name,url,token", _ENVS)
def test_request_shape_identical_across_envs(
    name: str, url: str, token: str, tmp_path: Path
) -> None:
    env = _env(name, url, token, tmp_path=tmp_path)
    img = Image.new("RGB", (40, 40), color=(1, 2, 3))
    with patch("uploader.requests.post", return_value=_ok()) as post:
        assert (
            upload_page(env, tmp_path / "scan.pdf", 1, 2, img)
            is UploadOutcome.ACCEPTED
        )

    kwargs = post.call_args.kwargs
    called_url = (
        post.call_args.args[0] if post.call_args.args else kwargs["url"]
    )
    # Only the base URL + X-API-Key value differ between environments.
    assert called_url == f"{url}/api/scanned-images/upload"
    assert kwargs["headers"] == {"X-API-Key": token}
    files = kwargs["files"]
    assert files[0][0] == "files"
    assert files[0][1][0] == "scan_p001.tiff"
    assert files[0][1][2] == "image/tiff"
    assert kwargs["data"] == {}
