# pms-scanner Development Guidelines

Auto-generated from all feature plans. Last updated: 2026-05-15

## Active Technologies

- Python 3.12 (003-we-watch-all, 004-multi-env-uploads)
- PyMuPDF (fitz), Pillow, pytesseract — PDF rendering + OSD orientation
- FastAPI, uvicorn — per-machine dashboard + SSE
- APScheduler — cron scheduling; one job per (machine, environment) with `max_instances=1` + `coalesce=True` (004-multi-env-uploads)
- requests — backend uploads
- ntplib — NTP offset measurement; out-of-band privileged helper for clock correction (004-multi-env-uploads)

## Project Structure

```text
scanner/        # main package (batch, dashboard, pdf_processor, uploader, state, config, machine, ntp, scheduler)
tests/unit/
tests/integration/
tests/contract/
launchd/        # io.mpsinc.pms-scanner.plist (macOS LaunchAgent)
systemd/        # pms-scanner.service (Linux user unit, 004-multi-env-uploads)
docs/           # launchd-setup.md, systemd-setup.md (004-multi-env-uploads)
```

## Commands

```sh
pytest --ignore=tests/contract --cov=scanner
ruff check .
mypy --strict scanner/
python -m scanner   # run locally
```

## Code Style

Python 3.12: Follow standard conventions; ruff + mypy --strict enforced. OS-specific behavior confined to clearly bounded modules and feature-detected where possible (per Constitution v3.0.0 Principle I).

## Deployment

- **macOS**: launchd `LaunchAgent` (`launchd/io.mpsinc.pms-scanner.plist`) with `KeepAlive: true` and `WaitForPaths` listing every env's `WATCH_DIR`. See `docs/launchd-setup.md`.
- **Linux (NUC)**: systemd `--user` unit (`systemd/pms-scanner.service`) with `Restart=always` and `RequiresMountsFor=` listing every env's `WATCH_DIR`. Main process runs unprivileged; clock-correction helper installed separately with narrow sudoers. See `docs/systemd-setup.md`.
- Dashboard runs on `http://<machine-ip>:8080` per machine; no cross-machine aggregation.

## Recent Changes

- 004-multi-env-uploads: dual-environment routing (`production` → `adg.mpsinc.io`, `staging` → `dev.adg.mpsinc.io`) with per-environment credentials; multi-machine fleet (`macmini`, `nuc`) sharing SMB watch folders with per-machine `in-progress/<machine>/` subfolders and atomic-rename claims; staggered :00/:15/:30/:45 schedule; NTP-gated startup + recurring drift check; Constitution amended to v3.0.0 (cross-platform macOS + Linux).
- 003-we-watch-all: macOS-native PDF batch processor with cron scheduling and
  live dashboard; replaced watchdog watcher with APScheduler + FastAPI SSE.

<!-- MANUAL ADDITIONS START -->
<!-- MANUAL ADDITIONS END -->
