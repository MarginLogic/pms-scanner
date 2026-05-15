# Quickstart: Dual-Environment Fleet Bring-Up

**Branch**: `004-multi-env-uploads` | **Date**: 2026-05-15
**Audience**: an operator (or developer) standing up the two-machine fleet from scratch.

This walks through bringing up `macmini` and `nuc` against shared SMB watch folders, end-to-end. Expect ~30 minutes for the first machine, ~10 minutes for the second.

---

## 0. Prerequisites (both machines)

- Python 3.12 installed (`brew install python@3.12` on macOS; `apt install python3.12` on Debian/Ubuntu).
- `tesseract` available (`brew install tesseract` / `apt install tesseract-ocr`) — needed by `pdf_processor.py`'s OSD fallback.
- The SMB shares for production and staging mounted at the per-OS conventional paths:
  - macOS: `/Volumes/aria/ARIAscans-prod`, `/Volumes/aria/ARIAscans-staging` (mount via Finder → "Connect to Server").
  - Linux: `/mnt/aria/ARIAscans-prod`, `/mnt/aria/ARIAscans-staging` (mount via `/etc/fstab` with `_netdev,x-systemd.automount` or a dedicated systemd `.mount` unit).
- Each machine's clock is roughly correct (within a few minutes; the NTP gate enforces ±1 s afterward).
- Operator has API tokens for **both** environments (one for `adg.mpsinc.io`, one for `dev.adg.mpsinc.io`).

---

## 1. Install pms-scanner on `macmini`

```sh
git clone https://github.com/mpsinc/pms-scanner.git
cd pms-scanner
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Create `~/.config/pms-scanner/.env`:

```env
MACHINE_IDENTITY=macmini

NTP__SOURCE=pool.ntp.org
NTP__MAX_DRIFT_SECONDS=1.0
NTP__CHECK_INTERVAL_SECONDS=3600

ENVIRONMENTS=production,staging

ENV_PRODUCTION__WATCH_DIR=/Volumes/aria/ARIAscans-prod
ENV_PRODUCTION__BACKEND_BASE_URL=https://adg.mpsinc.io
ENV_PRODUCTION__API_TOKEN=<prod-token>
ENV_PRODUCTION__SCHEDULE_OFFSET_SECONDS=0

ENV_STAGING__WATCH_DIR=/Volumes/aria/ARIAscans-staging
ENV_STAGING__BACKEND_BASE_URL=https://dev.adg.mpsinc.io
ENV_STAGING__API_TOKEN=<staging-token>
ENV_STAGING__SCHEDULE_OFFSET_SECONDS=15

DASHBOARD_PORT=8080
LOG_LEVEL=INFO
```

(Optional, only if you want the daemon to actively correct clock drift instead of just warning when the OS time-sync is off — install the privileged helper):

```sh
sudo cp scripts/macos/pms-scanner-correct-clock /usr/local/libexec/
sudo chmod 755 /usr/local/libexec/pms-scanner-correct-clock
# add a narrowly scoped sudoers entry — see docs/launchd-setup.md
```

---

## 2. Smoke test on `macmini`

Run in foreground first:

```sh
python -m scanner
```

Expected log lines (timestamps elided):

```
INFO scanner.config: loaded config — machine=macmini envs=[production, staging]
INFO scanner.ntp: ntp gate ok — source=pool.ntp.org offset=+0.043s
INFO scanner.dashboard: serving on http://0.0.0.0:8080
INFO scanner.scheduler: registered job production at second=0 (macmini)
INFO scanner.scheduler: registered job staging at second=15 (macmini)
```

Open `http://<macmini-ip>:8080` in a browser — the dashboard shows both
environments idling.

Drop a multi-page PDF into `/Volumes/aria/ARIAscans-prod/`. At the next `HH:MM:00`, you should see in the log:

```
INFO scanner.batch: [machine=macmini env=production] claimed test.pdf → in-progress/macmini/
INFO scanner.uploader: [machine=macmini env=production] uploading page 1/33 → https://adg.mpsinc.io/api/scanned-images/upload
...
INFO scanner.batch: [machine=macmini env=production] processed test.pdf
```

Drop another PDF into `/Volumes/aria/ARIAscans-staging/`. At the next `HH:MM:15`, you should see the same flow but with `env=staging` and `https://dev.adg.mpsinc.io/...`.

`Ctrl-C` to stop.

---

## 3. Install as a daemon on `macmini`

```sh
cp launchd/io.mpsinc.pms-scanner.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/io.mpsinc.pms-scanner.plist
launchctl enable gui/$UID/io.mpsinc.pms-scanner
```

Verify with `launchctl print gui/$UID/io.mpsinc.pms-scanner | head -20`. The plist
includes a `WaitForPaths` entry for both watch directories — startup is
deferred until both SMB shares are mounted.

See `docs/launchd-setup.md` for plist contents.

---

## 4. Install pms-scanner on `nuc`

Same code, different config and supervisor.

```sh
git clone https://github.com/mpsinc/pms-scanner.git
cd pms-scanner
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e .
```

`~/.config/pms-scanner/.env` differs only in **machine identity, watch-dir
paths, and offsets**:

```env
MACHINE_IDENTITY=nuc

# NTP block: identical to macmini

ENVIRONMENTS=production,staging

ENV_PRODUCTION__WATCH_DIR=/mnt/aria/ARIAscans-prod
ENV_PRODUCTION__BACKEND_BASE_URL=https://adg.mpsinc.io
ENV_PRODUCTION__API_TOKEN=<prod-token>           # SAME token as macmini
ENV_PRODUCTION__SCHEDULE_OFFSET_SECONDS=30       # ← nuc-specific

ENV_STAGING__WATCH_DIR=/mnt/aria/ARIAscans-staging
ENV_STAGING__BACKEND_BASE_URL=https://dev.adg.mpsinc.io
ENV_STAGING__API_TOKEN=<staging-token>
ENV_STAGING__SCHEDULE_OFFSET_SECONDS=45          # ← nuc-specific

DASHBOARD_PORT=8080
LOG_LEVEL=INFO
```

Install the systemd unit:

```sh
mkdir -p ~/.config/systemd/user
cp systemd/pms-scanner.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pms-scanner.service
systemctl --user status pms-scanner.service
```

The unit declares `RequiresMountsFor=/mnt/aria/ARIAscans-prod /mnt/aria/ARIAscans-staging` so the service waits for both mounts.

See `docs/systemd-setup.md` for unit contents.

---

## 5. Verify the fleet stride

With both machines running, drop a PDF into each of the **production** and
**staging** watch folders. Observe (either from `journalctl --user -u pms-scanner` on nuc, `~/Library/Logs/io.mpsinc.pms-scanner.log` on macmini, or the dashboards) that within one minute you see **four** poll events:

- macmini production at `HH:MM:00`
- macmini staging at `HH:MM:15`
- nuc production at `HH:MM:30`
- nuc staging at `HH:MM:45`

And nothing in between. (SC-007, SC-008a.)

---

## 6. Verify routing isolation

Place a single PDF in `/Volumes/aria/ARIAscans-prod/` only. Tail both backends'
ingest logs (or use the admin UI). Confirm:

- Every page lands in production `adg.mpsinc.io`.
- **Zero pages** land in staging `dev.adg.mpsinc.io`. (SC-001, SC-002.)

Repeat with a PDF in staging only — invert the expectation.

---

## 7. Verify per-machine claim isolation

Drop **10 PDFs** in the production folder. Both machines are running. After
one minute:

- Count files in `<prod>/in-progress/macmini/` plus `<prod>/in-progress/nuc/` = 0 (all completed).
- Count files in `<prod>/processed/` = 10.
- No file appears in `processed/` twice. (SC-009, SC-010.)

Mid-run snapshot (during the minute): `ls in-progress/macmini/ in-progress/nuc/` shows files split between the two subfolders, never the same filename in both.

---

## 8. Verify crash recovery is per-machine

While `nuc` is processing a file, kill `nuc` (`systemctl --user stop pms-scanner` on the box). Verify a file remains in `<prod>/in-progress/nuc/`. macmini continues running and **does not** touch that file. Restart `nuc`; it picks the file back up and processes it on the next poll. (SC-011.)

---

## 9. Verify the NTP gate

Stop pms-scanner. Manually advance the clock by 30 seconds (`sudo date -s …`).
Start pms-scanner. Expected: startup refuses with a log line naming the
measured offset and the NTP source (FR-022 / SC-014). Fix the clock (or let
the system time-sync correct it) and retry.

---

## Done

The fleet now satisfies every measurable success criterion in the spec (SC-001 through SC-014). The next time you want to add a third machine, all you need is:

1. Pick a unique `MACHINE_IDENTITY` (e.g., `lab`).
2. Pick two unused schedule offsets across the fleet (e.g., `7` and `22`).
3. Provision the same SMB mounts and run from step 1 above.
