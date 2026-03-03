# sdcard-auto-copy

Automated SD card ingestion for Linux. Insert a card, files are copied to NAS,
progress is visible in a web UI. Remove the card when done.

## How it works

```
SD card inserted
      │
      ▼
[kernel block event]
      │
      ▼
99-sdcard-mount.rules        ← udev rule fires a systemd-run to escape
      │                         the udev sandbox (mount(2) is blocked inside it)
      ▼
sdcard-mount.sh              ← mounts the card, launches the copy job
      │
      ├──► sdcard-copy.py    ← copies files, sends webhook events throughout
      │          │
      │          └──► HTTP webhooks (card_inserted, copy_progress,
      │                              copy_finished, card_removed, …)
      │                     │
      │                     ▼
      │              sdcard-webui.py   ← receives webhooks, streams live
      │                                  progress to the browser via SSE
      ▼
card removed / copy finished
      │
      ▼
sdcard-mount.sh              ← lazy-unmounts the card, removes the mount point
```

## Components

### `extras/99-sdcard-mount.rules`

udev rule that detects block device add/remove events for SD cards and USB
card readers (`mmcblk*` and `sd[b-z]*` partitions). Rather than running the
mount script directly — which would fail because udev workers run in a
sandboxed cgroup where `mount(2)` is blocked — it escapes the sandbox using
`systemd-run`, spawning `sdcard-mount.sh` in an unrestricted transient scope.

Deploy to: `/etc/udev/rules.d/99-sdcard-mount.rules`

Reload after changes:
```bash
sudo udevadm control --reload-rules
```

---

### `mount/sdcard-mount.sh`

Orchestrates everything that happens when a card is inserted or removed.

**On insert:**
1. Waits for the device node to settle
2. Detects the filesystem type (`vfat`, `exfat`, `ntfs`, `ext*`) and mounts to `/mnt/media/<kernel_dev>`
3. Launches `sdcard-copy.py` in a new `systemd-run` scope (asynchronously, so the mount script exits immediately)

**On remove:**
1. Lazy-unmounts the card (`umount -l`)
2. Removes the mount point directory

All events are logged to `/var/log/sdcard-mount.log`.

Deploy to: `/usr/local/bin/sdcard-mount.sh` (mode `0755`)

---

### `copy/sdcard-copy.py`

The main copy daemon. Launched by `sdcard-mount.sh` for each card insertion,
runs for the duration of the copy, then exits.

**Key behaviours:**

- **Camera detection** — reads the `.camera_detect_report.json` written by
  `detect-camera-content.sh` (if present) to determine which camera profile to
  apply. Falls back to scanning file extensions and EXIF data directly.
- **Resumable copies** — tracks copied files in a per-card state file keyed by
  filesystem UUID (`/var/lib/sdcard-copy/<uuid>.state.json`). Reinserting the
  same card (even in a different reader, on a different day) skips already-
  verified files.
- **Verified writes** — files are written to a `.part` temporary, then renamed
  atomically on success. Chunk hashes are verified before the rename.
- **Parallel copies** — configurable per-camera thread pool for fast NAS writes.
- **Removal detection** — a background thread polls the raw block device with
  `O_DIRECT` every 2 seconds. Three consecutive failures trigger a graceful
  abort (USB readers don't always fire a remove udev event when the card is
  pulled).
- **Webhooks** — HTTP POST events sent throughout the copy lifecycle (see
  Webhooks below).

Deploy to: `/usr/local/bin/sdcard-copy.py` (mode `0755`)

**Configuration:** `copy/config.toml` → `/etc/sdcard-copy/config.toml`

Camera destinations use placeholders:

| Placeholder | Value |
|---|---|
| `{year}` `{month}` `{day}` | Date the copy started |
| `{date}` | `YYYY-MM-DD` |
| `{camera}` | Camera profile name (e.g. `GoPro`) |
| `{exif_camera}` | Make+Model from EXIF |
| `{card_uuid}` | Filesystem UUID |
| `{card_label}` | Filesystem label |

Example camera section:
```toml
[cameras.GoPro]
destination       = "/mnt/nas/footage/gopro/{year}/{month}/{day}/{exif_camera}"
delete_after_copy = false
extensions        = []           # empty = copy everything
```

**Directory bootstrap** — run once after a fresh install:
```bash
sudo cp copy/sdcard-copy.tmpfiles /etc/tmpfiles.d/sdcard-copy.conf
sudo systemd-tmpfiles --create
```

This creates `/etc/sdcard-copy`, `/var/lib/sdcard-copy`, and
`/var/log/camera-detection` before the first card is ever inserted.

**Log rotation:**
```bash
sudo cp copy/sdcard-copy.logrotate /etc/logrotate.d/sdcard-copy
```

---

### Webhooks

`sdcard-copy.py` sends HTTP POST events to the URLs configured in
`config.toml`. All six event types post to the same URL or to separate URLs —
your choice. The `sdcard-webui.py` exposes a single `/webhook` endpoint that
handles all of them.

| Event | When |
|---|---|
| `card_inserted` | Card mounted, cameras identified |
| `copy_started` | First file about to be copied for a camera |
| `copy_progress` | Every `progress_interval_seconds` (default 30) |
| `copy_finished` | All files for a camera done — includes `files_copied`, `files_skipped`, `files_errored`, `files_deleted` |
| `copy_failed` | Unrecoverable error |
| `card_removed` | Card pulled (detected via udev event or `O_DIRECT` poll) |

Point all six at the web UI:
```toml
[webhooks]
card_inserted  = "http://localhost:7777/webhook"
copy_started   = "http://localhost:7777/webhook"
copy_progress  = "http://localhost:7777/webhook"
copy_finished  = "http://localhost:7777/webhook"
copy_failed    = "http://localhost:7777/webhook"
card_removed   = "http://localhost:7777/webhook"
progress_interval_seconds = 5
```

---

### `webui/sdcard-webui.py` + `webui/compose.yaml`

A Flask web UI that receives the webhooks and streams live progress to any
connected browser using Server-Sent Events (SSE).

**Features:**
- Live progress bars with ETA per camera
- Per-camera stats: copied (green), skipped (grey), errored (red), deleted (orange)
- Job outcome on completion: *Finished successfully*, *Finished with errors*, or *Card removed before copy finished*
- History of recent cards
- Auto-reconnects if the server restarts

**Running with Docker Compose:**
```bash
cd webui
# Set Python version in .env (default: 3.11)
docker compose up -d
```

The web UI is then available at `http://<host>:7777`.

Gunicorn is used as the WSGI server with a single `gthread` worker and 16
threads. A single worker is required because in-process state (active cards,
SSE client queues) must not be forked across multiple workers.

---

## Extras (not currently used)

### `extras/detect-camera-content.sh`

Four-layer camera detection: directory structure → file extensions → filename
patterns → EXIF data. Writes a `.camera_detect_report.json` to the card root
and to `/var/log/camera-detection/`. `sdcard-copy.py` reads this report if
present but also performs its own detection, so this script is optional.

Deploy to: `/usr/local/bin/detect-camera-content.sh` (mode `0755`)

Log rotation: `sudo cp extras/camera-detection.logrotate /etc/logrotate.d/camera-detection`

### `extras/sdcard-watchdog.sh`

Standalone watchdog that polls a raw block device with `dd`/`O_DIRECT` every
3 seconds and triggers a lazy unmount after three consecutive failures. Useful
for USB card readers that don't signal removal via udev. `sdcard-copy.py`
includes the same logic internally via its `RemovalDetector` class, so the
watchdog is redundant when the copy script is active.

---

## Monitoring

```bash
# Follow the mount/unmount log
tail -f /var/log/sdcard-mount.log

# Follow the copy log
tail -f /var/log/sdcard-copy.log

# List active copy scopes
systemctl list-units 'run-*.scope'

# Stream a specific copy job's output
journalctl -f _SYSTEMD_UNIT=run-XXXXX.scope

# Inspect saved card state
cat /var/lib/sdcard-copy/<uuid>.state.json | python3 -m json.tool

# Force a full re-copy for a card
rm /var/lib/sdcard-copy/<uuid>.state.json
```

## Deployment summary

| File | Deploy to | Mode |
|---|---|---|
| `mount/sdcard-mount.sh` | `/usr/local/bin/` | `0755` |
| `copy/sdcard-copy.py` | `/usr/local/bin/` | `0755` |
| `copy/config.toml` | `/etc/sdcard-copy/config.toml` | `0640` |
| `copy/sdcard-copy.tmpfiles` | `/etc/tmpfiles.d/sdcard-copy.conf` | `0644` |
| `copy/sdcard-copy.logrotate` | `/etc/logrotate.d/sdcard-copy` | `0644` |
| `extras/99-sdcard-mount.rules` | `/etc/udev/rules.d/` | `0644` |
| `extras/detect-camera-content.sh` | `/usr/local/bin/` *(optional)* | `0755` |
| `extras/camera-detection.logrotate` | `/etc/logrotate.d/camera-detection` *(optional)* | `0644` |
| `webui/` | run in place with `docker compose up -d` | — |

## License

sdcard-auto-copy is licensed under GPL 3.0. See [here](LICENSE).
