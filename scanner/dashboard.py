"""
FastAPI dashboard for real-time batch progress.

Endpoints
---------
GET  /         Inline single-page HTML dashboard
GET  /status   JSON snapshot of current and last batch run
GET  /events   Server-Sent Events stream (run/file/page events + heartbeats)
POST /run      Manually trigger a batch run immediately
GET  /healthz  Health check
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from .config import AppSettings
from .state import AppState, BatchRunState
from .state import app_state as _default_app_state

logger = logging.getLogger(__name__)

app = FastAPI(title="pms-scanner dashboard")

# ---------------------------------------------------------------------------
# State injection — tests can replace this
# ---------------------------------------------------------------------------

_app_state: AppState = _default_app_state

# 004 runtime injection — set by __main__/tests via configure().
_settings: AppSettings | None = None
_run_state: BatchRunState | None = None


def configure(settings: AppSettings, state: BatchRunState) -> None:
    """Wire the multi-env runtime into the dashboard (called by __main__)."""
    global _settings, _run_state
    _settings = settings
    _run_state = state


def emit_clock_event(event: dict[str, object]) -> None:
    """Push a clock_sync / clock_drift_warning event onto the SSE stream."""
    _app_state.emit_event(event)


def _per_env_run(state: BatchRunState, name: str) -> dict[str, object] | None:
    st = state.env(name)
    if st.last_run_started_at is None:
        return None
    return {
        "environment": name,
        "current_file": st.current_file,
        "current_page": st.current_page,
        "total_pages": st.total_pages,
        "files_processed": st.files_processed,
        "pages_uploaded": st.pages_uploaded,
        "errors": [
            {
                "filename": e.filename,
                "message": e.message,
                "page_num": e.page_num,
                "at": e.at.isoformat(),
            }
            for e in st.errors
        ],
        "started_at": st.last_run_started_at.isoformat(),
        "finished_at": (
            st.last_run_finished_at.isoformat()
            if st.last_run_finished_at
            else None
        ),
    }


def _multi_env_status() -> dict[str, object]:
    assert _settings is not None and _run_state is not None
    sync = _run_state.recent_clock_sync
    warn = _run_state.last_drift_warning
    environments: dict[str, object] = {}
    for env in _settings.environments:
        st = _run_state.env(env.name)
        active = st.current_file is not None
        run = _per_env_run(_run_state, env.name)
        environments[env.name] = {
            "enabled": env.enabled,
            "schedule_offset_seconds": env.schedule_offset_seconds,
            "backend_base_url": env.backend_base_url,
            "current_run": run if active else None,
            "last_run": run if not active else None,
        }
    return {
        "machine": _settings.machine.name,
        "ntp": {
            "source": sync.source if sync else _settings.ntp.source,
            "last_measured_at": (
                sync.measured_at.isoformat() if sync else None
            ),
            "offset_seconds": sync.offset_seconds if sync else None,
            "outcome": sync.outcome if sync else None,
            "last_drift_warning": (
                {
                    "source": warn.source,
                    "offset_seconds": warn.offset_seconds,
                    "outcome": warn.outcome,
                    "correction_exit_code": warn.correction_exit_code,
                    "measured_at": warn.measured_at.isoformat(),
                }
                if warn
                else None
            ),
        },
        "environments": environments,
    }

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the dashboard HTML page."""
    return HTMLResponse(content=_DASHBOARD_HTML, status_code=200)


@app.get("/status")
async def status() -> JSONResponse:
    """Per-(machine, env) snapshot + latest NTP record (dashboard-events.md)."""
    if _settings is None or _run_state is None:
        return JSONResponse(
            {"detail": "dashboard runtime not configured"}, status_code=503
        )
    return JSONResponse(_multi_env_status())


@app.get("/events")
async def events() -> StreamingResponse:
    """
    Server-Sent Events stream.

    Sends queued events from AppState.event_queue plus a heartbeat every 15 s.
    """
    logger.debug("SSE client connected")

    async def generate() -> AsyncGenerator[str, None]:
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        _app_state.event_queue.get(), timeout=15.0
                    )
                    event_type = event.get("type", "message")
                    data = json.dumps(event)
                    yield f"event: {event_type}\ndata: {data}\n\n"
                except TimeoutError:
                    # Heartbeat to keep proxies from closing idle connections
                    yield "event: heartbeat\ndata: {}\n\n"
        except asyncio.CancelledError:
            logger.debug("SSE client disconnected")
            raise

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _run_env_once(env_name: str) -> str:
    """Build a BatchRunner for ``env_name`` and execute one synchronous pass."""
    from .batch import BatchRunner
    from .notify import Notifier

    assert _settings is not None and _run_state is not None
    env = next(e for e in _settings.environments if e.name == env_name)
    runner = BatchRunner(
        env,
        _settings.machine,
        _run_state,
        settle_seconds=_settings.file_settle_seconds,
        upload_timeout_seconds=_settings.upload_timeout_seconds,
        upload_max_retries=_settings.upload_max_retries,
        upload_retry_max_wait_seconds=_settings.upload_retry_max_wait_seconds,
        emit=_app_state.emit_event,
        notifier=Notifier(_settings.notify),
    )
    run_id = str(uuid.uuid4())
    logger.info(
        "Manual /run env=%s machine=%s run_id=%s",
        env_name,
        _settings.machine.name,
        run_id,
    )
    runner.run_once()
    return run_id


@app.post("/run", status_code=202)
async def manual_run(environment: str | None = None) -> JSONResponse:
    """Manually trigger a run on this machine.

    ``?environment=<name>`` runs that single env; unknown env → 404.
    Without the param, every enabled env runs concurrently. Returns
    202 ``{machine, triggered, run_ids}``.
    """
    if _settings is None or _run_state is None:
        return JSONResponse(
            {"detail": "dashboard runtime not configured"}, status_code=503
        )

    if environment is not None:
        names = [e.name for e in _settings.environments]
        if environment not in names:
            return JSONResponse(
                {
                    "detail": (
                        f"environment '{environment}' not configured on "
                        f"this machine"
                    )
                },
                status_code=404,
            )
        targets = [environment]
    else:
        targets = [e.name for e in _settings.enabled_environments]

    run_ids = await asyncio.gather(
        *(run_in_threadpool(_run_env_once, name) for name in targets)
    )
    return JSONResponse(
        {
            "machine": _settings.machine.name,
            "triggered": targets,
            "run_ids": dict(zip(targets, run_ids, strict=True)),
        },
        status_code=202,
    )


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# Inline dashboard HTML
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PMS Scanner — multi-env</title>
<style>
  body { font-family: monospace; background: #1a1a2e; color: #e0e0e0;
         padding: 20px; }
  h1 { color: #00d4ff; margin-bottom: 4px; }
  .banner { background: #16213e; border: 1px solid #0f3460;
            border-radius: 6px; padding: 10px 14px; margin: 10px 0; }
  .banner b { color: #00d4ff; }
  .ntp-ok { color: #3ddc84; }
  .ntp-warn { color: #e94560; }
  .panes { display: flex; gap: 16px; flex-wrap: wrap; }
  .pane { flex: 1 1 360px; background: #16213e;
          border: 1px solid #0f3460; border-radius: 6px; padding: 14px; }
  .pane h2 { color: #00d4ff; margin: 0 0 6px 0; font-size: 1.1em; }
  .pane .meta { color: #aaa; font-size: 0.82em; margin-bottom: 8px; }
  .pane.disabled { opacity: 0.5; }
  .kv { display: flex; justify-content: space-between;
        border-bottom: 1px solid #0f3460; padding: 3px 0; font-size: 0.9em; }
  .file-name { color: #e94560; font-weight: bold; }
  .progress-bar { background: #0f3460; height: 6px; border-radius: 3px;
                  margin-top: 6px; overflow: hidden; }
  .progress-fill { background: #00d4ff; height: 100%;
                   transition: width 0.25s; }
  .errs { color: #e94560; font-size: 0.82em; margin-top: 6px; }
  button { background: #0f3460; color: #e0e0e0; border: 1px solid #00d4ff;
           padding: 6px 12px; cursor: pointer; border-radius: 4px; }
  button:hover { background: #00d4ff; color: #1a1a2e; }
  .empty { color: #666; font-style: italic; }
</style>
</head>
<body>
<!-- Per-machine view. One pane per environment: production, staging. -->
<h1>PMS Scanner</h1>
<div class="banner">
  machine <b id="machine">…</b> ·
  <span id="ntp">NTP …</span> ·
  <button id="run-btn">&#9654; Run all envs now</button>
</div>
<div class="panes" id="panes"></div>

<script>
const machineEl = document.getElementById('machine');
const ntpEl = document.getElementById('ntp');
const panesEl = document.getElementById('panes');

document.getElementById('run-btn').addEventListener('click', () => {
  fetch('/run', { method: 'POST' });
});

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
function kv(parent, k, v) {
  const row = el('div', 'kv');
  row.appendChild(el('span', null, k));
  row.appendChild(el('span', null, String(v)));
  parent.appendChild(row);
}

function buildPane(name, env) {
  const pane = el('div', 'pane' + (env.enabled ? '' : ' disabled'));
  pane.appendChild(el('h2', null, name));
  pane.appendChild(el('div', 'meta',
    'backend ' + env.backend_base_url +
    ' · poll :' + String(env.schedule_offset_seconds).padStart(2, '0') +
    (env.enabled ? '' : ' · DISABLED')));
  const run = env.current_run || env.last_run;
  if (!run) {
    pane.appendChild(el('div', 'empty', 'idle — no run yet'));
    return pane;
  }
  if (run.current_file) {
    pane.appendChild(el('div', 'file-name', run.current_file));
    const bar = el('div', 'progress-bar');
    const fill = el('div', 'progress-fill');
    const tot = run.total_pages || 0;
    fill.style.width = (tot ? (run.current_page / tot * 100) : 0) + '%';
    bar.appendChild(fill);
    pane.appendChild(bar);
  }
  kv(pane, 'files processed', run.files_processed || 0);
  kv(pane, 'pages uploaded', run.pages_uploaded || 0);
  kv(pane, 'errors', (run.errors || []).length);
  (run.errors || []).forEach(e =>
    pane.appendChild(el('div', 'errs',
      e.filename + (e.page_num ? ' p' + e.page_num : '') + ': ' + e.message)));
  return pane;
}

function renderNtp(ntp) {
  if (!ntp || ntp.outcome == null) { ntpEl.textContent = 'NTP …'; return; }
  const warn = ntp.last_drift_warning;
  ntpEl.className = warn ? 'ntp-warn' : 'ntp-ok';
  ntpEl.textContent = 'NTP ' + ntp.source + ' offset=' +
    (ntp.offset_seconds == null ? '?' : ntp.offset_seconds.toFixed(3)) +
    's (' + ntp.outcome + ')' + (warn ? ' ⚠ drift' : '');
}

function refresh() {
  fetch('/status').then(r => r.json()).then(data => {
    machineEl.textContent = data.machine || '?';
    renderNtp(data.ntp);
    const envs = data.environments || {};
    while (panesEl.firstChild) panesEl.removeChild(panesEl.firstChild);
    Object.keys(envs).sort().forEach(name =>
      panesEl.appendChild(buildPane(name, envs[name])));
  });
}

refresh();
// Poll fallback: keep the view honest even if SSE drops or no event
// arrives between scheduled runs.
setInterval(refresh, 10000);
const es = new EventSource('/events');
['run_started', 'file_started', 'page_done', 'file_done', 'run_done',
 'clock_sync', 'clock_drift_warning', 'counters_reset'].forEach(t =>
  es.addEventListener(t, () => refresh()));
es.addEventListener('heartbeat', () => {});
</script>
</body>
</html>
"""
