#!/usr/bin/env python3
"""
sdcard-copy.py — Automatic SD card copy daemon
Triggered by sdcard-mount.sh after a successful mount and camera detection.

Usage: sdcard-copy.py <mount_point> <kernel_dev> [--config /path/to/config.toml]

Design principles:
  - EXIF-first file routing: exiftool scans every file on the card in one batch
    call. Files are routed to camera configs based on Make/Model metadata.
    File extensions are a secondary filter applied on top of EXIF matching.
    Sidecar files with no EXIF (LRV, THM, SRT, …) are matched by extension alone
    and grouped with the camera type that owns them.
  - End-to-end integrity: SHA-256 is computed chunk-by-chunk while reading the
    source (zero extra I/O). After the atomic rename, the destination is re-read
    and its hash compared to the source hash. Chunk hashes are logged at DEBUG
    level; file hashes at INFO level and stored in the state file.
  - Parallel copies: cameras are copied concurrently (max_parallel_cameras).
    Within each camera, files are copied concurrently (max_parallel_files).
    All shared state (CardState, ProgressTracker, webhook throttle) is
    protected by per-object locks.
  - Resumable: state is keyed by filesystem UUID. On reinsertion the stored
    hash is compared to the current source hash — a changed file is re-copied.
  - Premature-removal safe: RemovalDetector reads 512 bytes from the raw device
    with O_DIRECT every 2 s. Three consecutive failures set a threading.Event
    that aborts all in-flight copies cleanly before saving state.
  - Atomic writes: every file goes through a .part sibling that is renamed only
    after the destination hash matches the source hash.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
import urllib.error
import urllib.request

try:
    import tomllib                      # type: ignore[import]  (stdlib >= 3.11)
except ImportError:
    try:
        import tomli as tomllib         # type: ignore[import]
    except ImportError:
        print(
            "ERROR: tomllib unavailable.\n"
            "  Python >= 3.11: it is part of the standard library.\n"
            "  Python <  3.11: pip install tomli",
            file=sys.stderr,
        )
        sys.exit(1)


# -----------------------------------------------------------------------------
# Default paths
# -----------------------------------------------------------------------------

DEFAULT_CONFIG    = Path("/etc/sdcard-copy/config.toml")
DEFAULT_STATE_DIR = Path("/var/lib/sdcard-copy")
DEFAULT_LOG       = Path("/var/log/sdcard-copy.log")
DEFAULT_ALGORITHM = "sha256"


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

def _make_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fmt    = logging.Formatter("%(asctime)s [%(levelname)-8s] %(message)s")
    logger = logging.getLogger("sdcard-copy")
    logger.setLevel(logging.DEBUG)

    fh = logging.FileHandler(log_path)
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)      # chunk hashes at DEBUG level go here

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)       # stdout -> systemd journal

    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = _make_logger(DEFAULT_LOG)


# -----------------------------------------------------------------------------
# EXIF scanning and camera-type mapping
# -----------------------------------------------------------------------------

# Maps lowercased substrings of "Make Model" EXIF strings to canonical camera
# type keys that match the [cameras."..."] sections in config.toml.
# Order matters: more-specific entries must precede general ones.
_EXIF_CAMERA_MAP: list[tuple[str, str]] = [
    ("gopro",        "GoPro"),
    ("dji",          "DJI Drone"),
    ("phase one",    "Phase One Camera"),
    ("phaseone",     "Phase One Camera"),
    ("hasselblad",   "Hasselblad Camera"),
    ("leica",        "Leica Camera"),
    ("ricoh",        "Ricoh/Pentax Camera"),
    ("pentax",       "Ricoh/Pentax Camera"),
    ("sigma",        "Sigma Camera"),
    ("om system",    "Olympus/OM System Camera"),
    ("om-system",    "Olympus/OM System Camera"),
    ("olympus",      "Olympus/OM System Camera"),
    ("fujifilm",     "Fujifilm Camera"),
    ("fuji",         "Fujifilm Camera"),
    ("nikon",        "Nikon DSLR/Mirrorless"),
    # Sony XAVC/cinema bodies (ILME-FX3, FX6, Venice ...) before generic Sony
    ("ilme",         "Sony Cinema/Mirrorless (XAVC)"),
    ("venice",       "Sony Cinema/Mirrorless (XAVC)"),
    ("sony",         "Sony Camera"),
    ("panasonic",    "Panasonic/Lumix Camera"),
    ("canon",        "Canon DSLR/Mirrorless"),
]

# Extension -> camera type for sidecar/proxy files that carry no EXIF.
_EXTENSION_FALLBACK: dict[str, str] = {
    "lrv": "GoPro",                        # GoPro low-res proxy
    "thm": "GoPro",                        # GoPro thumbnail sidecar
    "cr2": "Canon DSLR/Mirrorless",
    "cr3": "Canon DSLR/Mirrorless",
    "crw": "Canon DSLR/Mirrorless",
    "nef": "Nikon DSLR/Mirrorless",
    "nrw": "Nikon DSLR/Mirrorless",
    "arw": "Sony Camera",
    "srf": "Sony Camera",
    "sr2": "Sony Camera",
    "raf": "Fujifilm Camera",
    "orf": "Olympus/OM System Camera",
    "rw2": "Panasonic/Lumix Camera",
    "3fr": "Hasselblad Camera",
    "fff": "Hasselblad Camera",
    "iiq": "Phase One Camera",
}


def _make_to_camera_type(make: str, model: str) -> Optional[str]:
    combined = f"{make} {model}".lower().strip()
    for substring, camera_type in _EXIF_CAMERA_MAP:
        if substring in combined:
            return camera_type
    return None


@dataclass
class ExifInfo:
    make:        str = ""
    model:       str = ""
    camera_type: Optional[str] = None  # resolved camera-type key, or None
    file_type:   str = ""              # exiftool FileType e.g. "JPEG", "MP4"

    @property
    def exif_camera(self) -> str:
        """Sanitized Make+Model for use as a destination placeholder."""
        raw = f"{self.make} {self.model}".strip()
        if not raw:
            return "Unknown"
        return raw.replace("/", "_").replace("\\", "_").replace(" ", "_")


def _exiftool_available() -> bool:
    try:
        subprocess.run(["exiftool", "-ver"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def scan_exif_batch(files: list[Path]) -> dict[Path, ExifInfo]:
    """
    Run exiftool once over all files and return Path -> ExifInfo.
    Uses an argfile (-@) to avoid argv length limits on large cards.
    Falls back to an empty dict on any failure.
    """
    if not files:
        return {}

    if not _exiftool_available():
        log.warning(
            "exiftool not found — EXIF routing unavailable, using extension "
            "fallback only.  Install: apt install libimage-exiftool-perl"
        )
        return {}

    result: dict[Path, ExifInfo] = {}

    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False
        ) as argfile:
            argfile_path = Path(argfile.name)
            for f in files:
                argfile.write(str(f) + "\n")

        proc = subprocess.run(
            [
                "exiftool",
                "-json",       # machine-readable output
                "-fast2",      # skip trailer scanning
                "-Make",
                "-Model",
                "-FileType",
                "-SourceFile",
                "-@", str(argfile_path),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        argfile_path.unlink(missing_ok=True)

        if proc.returncode not in (0, 1):
            log.warning(f"exiftool returned {proc.returncode}: {proc.stderr[:200]}")

        if not proc.stdout.strip():
            return {}

        for rec in json.loads(proc.stdout):
            src = Path(rec.get("SourceFile", ""))
            if not src.is_absolute():
                continue
            make  = rec.get("Make",     "").strip()
            model = rec.get("Model",    "").strip()
            ftype = rec.get("FileType", "").strip()
            result[src] = ExifInfo(
                make        = make,
                model       = model,
                camera_type = _make_to_camera_type(make, model),
                file_type   = ftype,
            )

    except subprocess.TimeoutExpired:
        log.error("exiftool timed out — falling back to extension routing.")
    except json.JSONDecodeError as exc:
        log.error(f"Could not parse exiftool JSON: {exc}")
    except Exception as exc:
        log.error(f"exiftool scan failed: {exc}")

    return result


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

@dataclass
class CameraConfig:
    destination:       str
    delete_after_copy: bool      = False
    extensions:        list[str] = field(default_factory=list)
    # extensions: secondary filter applied after EXIF routing.
    # Empty = accept all file types that EXIF assigned to this camera.


@dataclass
class WebhookConfig:
    card_inserted:             Optional[str] = None
    copy_started:              Optional[str] = None
    copy_progress:             Optional[str] = None
    copy_finished:             Optional[str] = None
    copy_failed:               Optional[str] = None
    card_removed:              Optional[str] = None
    progress_interval_seconds: int = 30
    timeout_seconds:           int = 5


@dataclass
class Config:
    cameras:              dict[str, CameraConfig]
    webhooks:             WebhookConfig
    state_dir:            Path = field(default_factory=lambda: DEFAULT_STATE_DIR)
    log_path:             Path = field(default_factory=lambda: DEFAULT_LOG)
    verify_copy:          bool = True
    copy_buffer_bytes:    int  = 4 * 1024 * 1024
    hash_algorithm:       str  = DEFAULT_ALGORITHM
    max_parallel_cameras: int  = 2
    max_parallel_files:   int  = 4


def load_config(path: Path) -> Config:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    cameras: dict[str, CameraConfig] = {}
    for name, cam in raw.get("cameras", {}).items():
        if "destination" not in cam:
            raise ValueError(f"Camera '{name}' missing required key 'destination'")
        cameras[name] = CameraConfig(
            destination       = cam["destination"],
            delete_after_copy = bool(cam.get("delete_after_copy", False)),
            extensions        = [
                e.lower().lstrip(".") for e in cam.get("extensions", [])
            ],
        )

    wh = raw.get("webhooks", {})
    webhooks = WebhookConfig(
        card_inserted             = wh.get("card_inserted"),
        copy_started              = wh.get("copy_started"),
        copy_progress             = wh.get("copy_progress"),
        copy_finished             = wh.get("copy_finished"),
        copy_failed               = wh.get("copy_failed"),
        card_removed              = wh.get("card_removed"),
        progress_interval_seconds = int(wh.get("progress_interval_seconds", 30)),
        timeout_seconds           = int(wh.get("timeout_seconds", 5)),
    )

    buf_mb = int(raw.get("copy_buffer_mb", 4))
    if buf_mb < 1:
        raise ValueError("copy_buffer_mb must be >= 1")

    algo = raw.get("hash_algorithm", DEFAULT_ALGORITHM).lower()
    try:
        hashlib.new(algo)
    except ValueError:
        raise ValueError(f"Unsupported hash_algorithm: {algo!r}")

    max_cams  = int(raw.get("max_parallel_cameras", 2))
    max_files = int(raw.get("max_parallel_files",   4))
    if max_cams  < 1: raise ValueError("max_parallel_cameras must be >= 1")
    if max_files < 1: raise ValueError("max_parallel_files must be >= 1")

    return Config(
        cameras              = cameras,
        webhooks             = webhooks,
        state_dir            = Path(raw.get("state_dir", DEFAULT_STATE_DIR)),
        log_path             = Path(raw.get("log_path",  DEFAULT_LOG)),
        verify_copy          = bool(raw.get("verify_copy", True)),
        copy_buffer_bytes    = buf_mb * 1024 * 1024,
        hash_algorithm       = algo,
        max_parallel_cameras = max_cams,
        max_parallel_files   = max_files,
    )


# -----------------------------------------------------------------------------
# Destination path resolution
# -----------------------------------------------------------------------------

# Placeholders:
#   {date}         2024-11-30
#   {year}         2024
#   {month}        11
#   {day}          30
#   {hour}         14
#   {minute}       05
#   {camera}       Canon_DSLR_Mirrorless   (config key, spaces/slashes -> _)
#   {exif_camera}  Canon_EOS_R5            (Make+Model from EXIF)
#   {card_uuid}    filesystem UUID
#   {card_label}   volume label or "unlabeled"

def resolve_destination(
    template:    str,
    camera:      str,
    exif_camera: str,
    card_uuid:   str,
    card_label:  str,
    now:         datetime,
) -> Path:
    safe_camera = camera.replace("/", "_").replace(" ", "_")
    try:
        resolved = template.format(
            date        = now.strftime("%Y-%m-%d"),
            year        = now.strftime("%Y"),
            month       = now.strftime("%m"),
            day         = now.strftime("%d"),
            hour        = now.strftime("%H"),
            minute      = now.strftime("%M"),
            camera      = safe_camera,
            exif_camera = exif_camera,
            card_uuid   = card_uuid,
            card_label  = card_label or "unlabeled",
        )
    except KeyError as exc:
        raise ValueError(
            f"Unknown placeholder {exc} in destination: {template!r}"
        ) from exc
    return Path(resolved)


# -----------------------------------------------------------------------------
# Card identity
# -----------------------------------------------------------------------------

def get_card_identity(device: str) -> tuple[str, str]:
    def blkid(field: str) -> str:
        try:
            r = subprocess.run(
                ["blkid", "-o", "value", "-s", field, device],
                capture_output=True, text=True, timeout=5,
            )
            return r.stdout.strip()
        except Exception as exc:
            log.warning(f"blkid({field}) failed for {device}: {exc}")
            return ""

    uuid  = blkid("UUID")
    label = blkid("LABEL")

    if not uuid:
        uuid = "nouuid-" + hashlib.sha1(device.encode()).hexdigest()[:12]
        log.warning(
            f"No filesystem UUID for {device}. "
            f"Using fallback {uuid!r}. Resume across readers may not work."
        )
    return uuid, label


# -----------------------------------------------------------------------------
# State management
# -----------------------------------------------------------------------------

@dataclass
class FileRecord:
    relative_path: str   # relative to mount root, forward slashes
    source_size:   int
    source_hash:   str   # "<algorithm>:<hexdigest>"
    destination:   str
    copied_at:     str


@dataclass
class CardState:
    card_uuid:      str
    card_label:     str
    device:         str
    first_inserted: str
    last_inserted:  str
    copied_files:   dict[str, FileRecord] = field(default_factory=dict)
    # Lock protects copied_files from concurrent worker thread writes
    _lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def is_copied(
        self,
        relative_path: str,
        expected_size: int,
        expected_hash: Optional[str],
    ) -> bool:
        """
        True iff the file was previously copied AND:
          - destination file still exists at the correct size, AND
          - if both stored and expected hashes are available, they match
            (guards against a re-used card with the same UUID)
        """
        with self._lock:
            rec = self.copied_files.get(relative_path)
        if rec is None:
            return False
        if rec.source_size != expected_size:
            return False
        dest = Path(rec.destination)
        try:
            if dest.stat().st_size != expected_size:
                return False
        except OSError:
            return False
        if expected_hash and rec.source_hash and rec.source_hash != expected_hash:
            return False
        return True

    def mark_copied(
        self,
        relative_path: str,
        source_size:   int,
        source_hash:   str,
        destination:   Path,
    ) -> None:
        record = FileRecord(
            relative_path = relative_path,
            source_size   = source_size,
            source_hash   = source_hash,
            destination   = str(destination),
            copied_at     = datetime.now().isoformat(),
        )
        with self._lock:
            self.copied_files[relative_path] = record

    def get_hash(self, relative_path: str) -> Optional[str]:
        with self._lock:
            rec = self.copied_files.get(relative_path)
        return rec.source_hash if rec else None


def _state_path(state_dir: Path, card_uuid: str) -> Path:
    safe = card_uuid.replace("/", "_").replace("\\", "_")
    return state_dir / f"{safe}.state.json"


def load_state(
    state_dir:  Path,
    card_uuid:  str,
    card_label: str,
    device:     str,
) -> CardState:
    path = _state_path(state_dir, card_uuid)
    now  = datetime.now().isoformat()

    if path.exists():
        try:
            with open(path) as f:
                data = json.load(f)
            copied = {
                k: FileRecord(**v)
                for k, v in data.get("copied_files", {}).items()
            }
            state = CardState(
                card_uuid      = data["card_uuid"],
                card_label     = data.get("card_label", card_label),
                device         = device,
                first_inserted = data.get("first_inserted", now),
                last_inserted  = now,
                copied_files   = copied,
            )
            log.info(
                f"Resuming state for {card_uuid!r} — "
                f"{len(copied)} file(s) already copied."
            )
            return state
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            log.warning(f"State for {card_uuid!r} corrupt ({exc}) — starting fresh.")

    log.info(f"New card {card_uuid!r} — starting fresh state.")
    return CardState(
        card_uuid=card_uuid, card_label=card_label, device=device,
        first_inserted=now, last_inserted=now,
    )


def save_state(state_dir: Path, state: CardState) -> None:
    """
    Atomic write via a uniquely-named tmp file followed by rename.

    Using a fixed .tmp suffix caused a race when multiple worker threads
    called save_state() concurrently: thread A would write the tmp file,
    thread B would overwrite it, thread A would rename B's tmp to the final
    path, then thread B's rename would find nothing and raise ENOENT.

    tempfile.NamedTemporaryFile in the same directory gives each call a
    unique path. The rename is still atomic on POSIX: whichever thread
    renames last wins, and every intermediate rename leaves the state file
    in a fully consistent (if slightly stale) state — no partial writes
    are ever visible.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(state_dir, state.card_uuid)

    with state._lock:
        snapshot = dict(state.copied_files)

    data = {
        "card_uuid"     : state.card_uuid,
        "card_label"    : state.card_label,
        "device"        : state.device,
        "first_inserted": state.first_inserted,
        "last_inserted" : state.last_inserted,
        "copied_files"  : {
            k: {
                "relative_path": v.relative_path,
                "source_size"  : v.source_size,
                "source_hash"  : v.source_hash,
                "destination"  : v.destination,
                "copied_at"    : v.copied_at,
            }
            for k, v in snapshot.items()
        },
    }
    # delete=False lets us rename after close(); cleaned up in the except
    # block if anything goes wrong before the rename completes.
    tmp_fd = tempfile.NamedTemporaryFile(
        mode="w",
        dir=state_dir,
        prefix=path.stem + "-",
        suffix=".tmp",
        delete=False,
    )
    tmp_path = Path(tmp_fd.name)
    try:
        json.dump(data, tmp_fd, indent=2)
        tmp_fd.close()
        tmp_path.rename(path)
    except Exception:
        tmp_fd.close()
        tmp_path.unlink(missing_ok=True)
        raise


# -----------------------------------------------------------------------------
# Webhooks
# -----------------------------------------------------------------------------

def _post_webhook(url: str, event: str, payload: dict, timeout: int) -> None:
    body = json.dumps({"event": event, **payload}).encode()
    req  = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent"  : "sdcard-copy/2.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            log.debug(f"Webhook {event!r} -> {url}: HTTP {resp.status}")
    except urllib.error.URLError as exc:
        log.warning(f"Webhook {event!r} -> {url} failed: {exc.reason}")
    except Exception as exc:
        log.warning(f"Webhook {event!r} -> {url} unexpected error: {exc}")


class WebhookSender:
    def __init__(self, cfg: WebhookConfig, base: dict) -> None:
        self._cfg  = cfg
        self._base = base
        self._last_progress: dict[str, float] = {}
        self._lock = threading.Lock()

    def _send(self, url: Optional[str], event: str, extra: dict) -> None:
        if not url:
            return
        payload = {**self._base, "timestamp": datetime.now().isoformat(), **extra}
        _post_webhook(url, event, payload, self._cfg.timeout_seconds)

    def card_inserted(self, cameras: list[str]) -> None:
        self._send(self._cfg.card_inserted, "card_inserted",
                   {"cameras_detected": cameras})

    def copy_started(self, camera: str, total_files: int, total_bytes: int) -> None:
        self._send(self._cfg.copy_started, "copy_started", {
            "camera": camera, "total_files": total_files, "total_bytes": total_bytes,
        })

    def copy_progress(
        self,
        camera:      str,
        files_done:  int,
        files_total: int,
        bytes_done:  int,
        bytes_total: int,
        eta:         Optional[float],
        force:       bool = False,
    ) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._last_progress.get(camera, 0.0)
            due  = force or (now - last >= self._cfg.progress_interval_seconds)
            if due:
                self._last_progress[camera] = now
        if not due:
            return
        pct = round(100.0 * bytes_done / bytes_total, 1) if bytes_total else 0.0
        self._send(self._cfg.copy_progress, "copy_progress", {
            "camera"      : camera,
            "files_done"  : files_done,
            "files_total" : files_total,
            "bytes_done"  : bytes_done,
            "bytes_total" : bytes_total,
            "percent"     : pct,
            "eta_seconds" : round(eta) if eta is not None else None,
        })

    def copy_finished(
        self, camera: str,
        copied: int, skipped: int, errored: int, deleted: int, total_bytes: int,
    ) -> None:
        self._send(self._cfg.copy_finished, "copy_finished", {
            "camera"         : camera,
            "files_copied"   : copied,
            "files_skipped"  : skipped,
            "files_errored"  : errored,
            "files_deleted"  : deleted,
            "bytes_total"    : total_bytes,
        })

    def copy_failed(self, camera: str, reason: str) -> None:
        self._send(self._cfg.copy_failed, "copy_failed",
                   {"camera": camera, "reason": reason})

    def card_removed(self, copy_completed: bool) -> None:
        self._send(self._cfg.card_removed, "card_removed",
                   {"copy_completed": copy_completed})


# -----------------------------------------------------------------------------
# Premature-removal detection
# -----------------------------------------------------------------------------

def _disk_device(partition_device: str) -> str:
    """
    Derive the whole-disk device from a partition device path.

    We probe the disk device rather than the partition because:
      - The partition device (/dev/sdd1) cannot be opened with O_DIRECT
        while a filesystem is mounted on it on many kernel/fs combinations
        (vfat, exfat in particular return EBUSY or EINVAL).
      - The disk device (/dev/sdd) is never mounted directly and is always
        readable regardless of what the partition is doing.

    Examples:
      /dev/sdd1   -> /dev/sdd
      /dev/sdd12  -> /dev/sdd
      /dev/mmcblk0p1  -> /dev/mmcblk0
      /dev/mmcblk0p12 -> /dev/mmcblk0
    """
    # mmcblk0p1, nvme0n1p1 — partition suffix is 'p' followed by digits
    m = re.match(r"^(/dev/(?:mmcblk|nvme)\d+n?\d*)p\d+$", partition_device)
    if m:
        return m.group(1)
    # sda1, sdb12, hda1 — strip trailing digits
    m = re.match(r"^(/dev/[a-z]+)\d+$", partition_device)
    if m:
        return m.group(1)
    # Already a disk device or unrecognised — use as-is
    return partition_device


class RemovalDetector:
    """
    Polls the whole-disk block device to detect physical card removal.

    Strategy:
      - Read 512 bytes from the *disk* device (e.g. /dev/sdd), not the
        partition (/dev/sdd1).  The disk device is never mounted, so the
        read always hits hardware and is never refused by the kernel.
      - Plain O_RDONLY without O_DIRECT avoids the alignment requirements
        that O_DIRECT imposes on the userspace buffer (EINVAL on some
        kernel/device combinations).  The kernel page cache for the disk
        device is irrelevant — we are only testing whether the hardware
        still responds, not reading meaningful data.
      - Three consecutive IOErrors (ENODEV, EIO, ENXIO) indicate the card
        reader has lost the medium.  A single failure is not enough because
        some readers emit a transient error during bus re-enumeration.
    """

    CONSECUTIVE_THRESHOLD = 3
    POLL_INTERVAL         = 2.0

    def __init__(self, partition_device: str, mount_point: Path) -> None:
        self._partition  = partition_device
        self._disk       = _disk_device(partition_device)
        self._mount      = mount_point
        self.removed     = threading.Event()
        self._stop       = threading.Event()
        self._thread     = threading.Thread(
            target=self._poll, daemon=True,
            name=f"removal:{partition_device}",
        )
        log.debug(
            f"[removal] watching disk={self._disk}  "
            f"partition={self._partition}  mount={self._mount}"
        )

    def start(self) -> None: self._thread.start()
    def stop(self)  -> None: self._stop.set()

    def _poll(self) -> None:
        failures = 0
        while not self._stop.is_set():
            gone = self._check_gone()
            if gone:
                failures += 1
                log.debug(
                    f"[removal] check failed "
                    f"({failures}/{self.CONSECUTIVE_THRESHOLD}): {gone}"
                )
                if failures >= self.CONSECUTIVE_THRESHOLD:
                    log.warning(
                        f"[removal] card gone after {failures} consecutive "
                        f"failures: {gone}"
                    )
                    self.removed.set()
                    return
            else:
                failures = 0
            time.sleep(self.POLL_INTERVAL)

    def _check_gone(self) -> Optional[str]:
        """
        Return None if the card is present, or a short reason string if gone.
        Uses two independent checks so a false positive in either alone is
        not enough to trigger removal.
        """
        # Check 1: read 512 bytes from the disk device.
        # Fails with ENODEV / EIO / ENXIO when the card is pulled.
        try:
            fd = os.open(self._disk, os.O_RDONLY)
            try:
                os.read(fd, 512)
            finally:
                os.close(fd)
        except OSError as exc:
            return f"disk read error: {exc.strerror} ({exc.errno})"

        # Check 2: verify the mount point is still in /proc/mounts.
        # Guards against the rare case where the disk device is still
        # accessible (USB reader re-enumerated) but the mount is gone.
        try:
            with open("/proc/mounts") as f:
                if not any(
                    str(self._mount) in line
                    for line in f
                ):
                    return f"mount point {self._mount} absent from /proc/mounts"
        except OSError as exc:
            # /proc/mounts being unreadable is a serious system problem,
            # not a card-removal event — don't count it as a failure.
            log.warning(f"[removal] cannot read /proc/mounts: {exc}")

        return None


# -----------------------------------------------------------------------------
# Hashing helpers
# -----------------------------------------------------------------------------

class ChunkHasher:
    """
    Computes a rolling whole-file hash chunk-by-chunk.
    Logs each chunk's individual hash at DEBUG level for auditability.
    """

    def __init__(self, algorithm: str, rel_path: str) -> None:
        self._algorithm = algorithm
        self._rel_path  = rel_path
        self._hasher    = hashlib.new(algorithm)
        self._offset    = 0

    def feed(self, chunk: bytes) -> None:
        self._hasher.update(chunk)
        chunk_digest = hashlib.new(self._algorithm, chunk).hexdigest()
        log.debug(
            f"CHUNK {self._algorithm}:{chunk_digest} "
            f"offset:{self._offset} size:{len(chunk)} "
            f"file:{self._rel_path}"
        )
        self._offset += len(chunk)

    def tagged(self) -> str:
        """Return '<algorithm>:<hexdigest>' for storage in state."""
        return f"{self._algorithm}:{self._hasher.hexdigest()}"


def hash_file(path: Path, algorithm: str, buf_size: int = 4 * 1024 * 1024) -> str:
    """Read an entire file and return '<algorithm>:<hexdigest>'."""
    h = hashlib.new(algorithm)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf_size), b""):
            h.update(chunk)
    return f"{algorithm}:{h.hexdigest()}"


# -----------------------------------------------------------------------------
# Progress tracker
# -----------------------------------------------------------------------------

class ProgressTracker:
    """Thread-safe byte/file counter with 60-second sliding-window ETA."""

    WINDOW = 60

    def __init__(self, total_bytes: int) -> None:
        self.total_bytes = total_bytes
        self._lock       = threading.Lock()
        self._bytes      = 0
        self._files      = 0
        self._samples: list[tuple[float, int]] = [(time.monotonic(), 0)]

    def add_bytes(self, n: int) -> None:
        with self._lock:
            self._bytes += n
            now = time.monotonic()
            self._samples.append((now, self._bytes))
            cutoff = now - self.WINDOW
            self._samples = [s for s in self._samples if s[0] >= cutoff]

    def file_done(self) -> None:
        with self._lock:
            self._files += 1

    @property
    def done_bytes(self) -> int:
        with self._lock: return self._bytes

    @property
    def done_files(self) -> int:
        with self._lock: return self._files

    def eta_seconds(self) -> Optional[float]:
        with self._lock:
            if len(self._samples) < 2:
                return None
            t0, b0 = self._samples[0]
            t1, b1 = self._samples[-1]
            dt = t1 - t0
            if dt <= 0:
                return None
            speed = (b1 - b0) / dt
            if speed <= 0:
                return None
            return max(0.0, (self.total_bytes - b1) / speed)


# -----------------------------------------------------------------------------
# Error types
# -----------------------------------------------------------------------------

class CardRemovedError(RuntimeError):
    pass

class HashMismatchError(RuntimeError):
    pass


# -----------------------------------------------------------------------------
# Atomic file copy with chunk hashing and destination verification
# -----------------------------------------------------------------------------

@dataclass
class CopyResult:
    source_hash:   str
    dest_hash:     str
    bytes_written: int


def copy_and_hash(
    src:       Path,
    dst:       Path,
    rel_path:  str,
    algorithm: str,
    buf_size:  int,
    removed:   threading.Event,
    on_chunk:  Optional[Callable[[int], None]] = None,
) -> CopyResult:
    """
    Copy src -> dst with full integrity checking:

      1. Read src in chunks; hash each chunk individually (logged at DEBUG)
         and accumulate a rolling whole-file hash.
      2. Write each chunk to dst.part, checking `removed` between chunks.
      3. Rename dst.part -> dst (atomic on POSIX).
      4. Re-read dst independently and compute its hash.
      5. Compare source hash with destination hash; delete dst and raise
         HashMismatchError on any discrepancy.

    The .part file is always cleaned up on any error path.
    Raises: CardRemovedError, HashMismatchError, OSError.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    part       = dst.with_suffix(dst.suffix + ".part")
    src_hasher = ChunkHasher(algorithm, rel_path)
    written    = 0

    try:
        with open(src, "rb") as fsrc, open(part, "wb") as fdst:
            for chunk in iter(lambda: fsrc.read(buf_size), b""):
                if removed.is_set():
                    raise CardRemovedError(
                        f"Card removed while copying {src.name}"
                    )
                src_hasher.feed(chunk)
                fdst.write(chunk)
                written += len(chunk)
                if on_chunk:
                    on_chunk(len(chunk))

        src_hash = src_hasher.tagged()
        log.info(f"FILE src  {src_hash}  {rel_path}")

        part.rename(dst)  # atomic rename

        dst_hash = hash_file(dst, algorithm, buf_size)
        log.info(f"FILE dst  {dst_hash}  {rel_path}")

        if src_hash != dst_hash:
            dst.unlink(missing_ok=True)
            raise HashMismatchError(
                f"{rel_path}: src={src_hash} dst={dst_hash}"
            )

        return CopyResult(src_hash, dst_hash, written)

    except (CardRemovedError, HashMismatchError):
        part.unlink(missing_ok=True)
        raise
    except OSError:
        part.unlink(missing_ok=True)
        if removed.is_set():
            raise CardRemovedError(
                f"Card removed (I/O error) while copying {src.name}"
            )
        raise


# -----------------------------------------------------------------------------
# File collection — EXIF first, extension fallback
# -----------------------------------------------------------------------------

@dataclass
class FileEntry:
    path:        Path
    size:        int
    exif:        ExifInfo
    camera_type: str   # resolved bucket for this file


def collect_all_files(
    mount_point: Path,
    exif_map:    dict[Path, ExifInfo],
) -> dict[str, list[FileEntry]]:
    """
    Walk mount_point and bucket every file by camera type.

    Per-file resolution order:
      1. EXIF Make/Model  -> camera type key
      2. Extension        -> _EXTENSION_FALLBACK camera type
      3. "Unknown"        (not copied unless explicitly configured)
    """
    buckets: dict[str, list[FileEntry]] = collections.defaultdict(list)

    for path in sorted(mount_point.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() == ".part":
            continue

        try:
            size = path.stat().st_size
        except OSError as exc:
            log.warning(f"Cannot stat {path}: {exc} — skipping.")
            continue

        exif  = exif_map.get(path, ExifInfo())
        ctype = exif.camera_type

        if ctype is None:
            ext   = path.suffix.lower().lstrip(".")
            ctype = _EXTENSION_FALLBACK.get(ext, "Unknown")

        buckets[ctype].append(
            FileEntry(path=path, size=size, exif=exif, camera_type=ctype)
        )

    for entries in buckets.values():
        entries.sort(key=lambda e: e.path)

    return dict(buckets)


def filter_by_extensions(
    entries:    list[FileEntry],
    extensions: list[str],   # empty = accept all
) -> list[FileEntry]:
    if not extensions:
        return entries
    allowed = set(extensions)
    return [
        e for e in entries
        if e.path.suffix.lower().lstrip(".") in allowed
    ]




# -----------------------------------------------------------------------------
# Per-camera copy job
# -----------------------------------------------------------------------------

class CameraJob:
    """Copies all files belonging to one camera type, with intra-camera parallelism."""

    def __init__(
        self,
        camera:      str,
        cam_cfg:     CameraConfig,
        entries:     list[FileEntry],
        mount_point: Path,
        card_uuid:   str,
        card_label:  str,
        config:      Config,
        state:       CardState,
        webhook:     WebhookSender,
        removed:     threading.Event,
    ) -> None:
        self.camera         = camera
        self.cam_cfg        = cam_cfg
        self.entries        = filter_by_extensions(entries, cam_cfg.extensions)
        self.mount_point    = mount_point
        self.config         = config
        self.state          = state
        self.webhook        = webhook
        self.removed        = removed
        # Freeze timestamp once so all files in this job share the same
        # date/time placeholders even if the job runs across midnight.
        self._now           = datetime.now()
        self._card_uuid     = card_uuid
        self._card_label    = card_label
        self._dest_template = cam_cfg.destination
        # For logging: unique Make+Model strings present in this job's files
        self._bodies: list[str] = sorted({
            e.exif.exif_camera
            for e in self.entries
            if e.exif.exif_camera != "Unknown"
        })

    def run(self) -> tuple[bool, int]:
        """
        Copy all files for this camera using a thread pool.
        Returns (all_ok, files_copied) where:
          - all_ok: True if every file succeeded (gates deletion)
          - files_copied: count of files newly written this session
            (skipped and failed files are not counted)
        """
        if not self.entries:
            log.info(f"[{self.camera}] No matching files on card.")
            return True, 0

        total_bytes = sum(e.size for e in self.entries)
        total_files = len(self.entries)
        tracker     = ProgressTracker(total_bytes)

        log.info(
            f"[{self.camera}] {total_files} files  "
            f"{total_bytes / 1_000_000:.1f} MB  "
            f"template={self._dest_template!r}  "
            f"bodies={self._bodies or ['Unknown']}"
        )
        self.webhook.copy_started(self.camera, total_files, total_bytes)

        counts      = {"copied": 0, "skipped": 0, "failed": 0}
        counts_lock = threading.Lock()

        def copy_one(entry: FileEntry) -> None:
            self._copy_entry(
                entry, tracker, counts, counts_lock, total_files, total_bytes
            )

        n_workers = min(self.config.max_parallel_files, total_files)
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=n_workers,
            thread_name_prefix=f"copy-{self.camera[:12]}",
        ) as pool:
            futures = {pool.submit(copy_one, e): e for e in self.entries}
            for fut in concurrent.futures.as_completed(futures):
                if self.removed.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise CardRemovedError(
                        f"Card removed during [{self.camera}] copy"
                    )
                try:
                    fut.result()
                except CardRemovedError:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as exc:
                    log.error(
                        f"[{self.camera}] Worker raised unexpected error: {exc}",
                        exc_info=True,
                    )

        camera_ok = counts["failed"] == 0

        # -- deletion ----------------------------------------------------------
        files_deleted = 0
        if camera_ok and self.cam_cfg.delete_after_copy:
            log.info(f"[{self.camera}] All files verified — deleting source files.")
            for entry in self.entries:
                rel = self._rel(entry.path)
                if self.state.is_copied(rel, entry.size, self.state.get_hash(rel)):
                    try:
                        entry.path.unlink()
                        files_deleted += 1
                        log.debug(f"  DELETED {rel}")
                    except OSError as exc:
                        log.warning(f"  Could not delete {rel}: {exc}")

        self.webhook.copy_finished(
            self.camera, counts["copied"], counts["skipped"],
            counts["failed"], files_deleted, tracker.done_bytes,
        )
        log.info(
            f"[{self.camera}] Done — "
            f"{counts['copied']} copied  "
            f"{counts['skipped']} skipped  "
            f"{counts['failed']} failed  "
            f"{files_deleted} deleted  "
            f"{tracker.done_bytes / 1_000_000:.1f} MB"
        )
        return camera_ok, counts["copied"]

    # -- internal --------------------------------------------------------------

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.mount_point).as_posix()
        except ValueError:
            return path.name

    def _copy_entry(
        self,
        entry:       FileEntry,
        tracker:     ProgressTracker,
        counts:      dict,
        counts_lock: threading.Lock,
        total_files: int,
        total_bytes: int,
    ) -> None:
        rel = self._rel(entry.path)

        # Resolve the destination for this specific file using its own
        # Make+Model so two bodies of the same maker land in separate folders
        # when the template contains {exif_camera}.
        dest_root = resolve_destination(
            self._dest_template, self.camera,
            entry.exif.exif_camera,
            self._card_uuid, self._card_label, self._now,
        )
        dst = dest_root / Path(rel)

        # -- resume check ------------------------------------------------------
        stored_hash = self.state.get_hash(rel)
        if self.state.is_copied(rel, entry.size, stored_hash):
            log.debug(f"  SKIP {rel}")
            with counts_lock: counts["skipped"] += 1
            tracker.add_bytes(entry.size)
            tracker.file_done()
            self.webhook.copy_progress(
                self.camera, tracker.done_files, total_files,
                tracker.done_bytes, total_bytes, tracker.eta_seconds(),
            )
            return

        log.info(
            f"  COPY [{self.camera}] {rel}  "
            f"({entry.size / 1_000_000:.2f} MB)"
        )

        # -- copy + hash -------------------------------------------------------
        try:
            result = copy_and_hash(
                src      = entry.path,
                dst      = dst,
                rel_path = rel,
                algorithm= self.config.hash_algorithm,
                buf_size = self.config.copy_buffer_bytes,
                removed  = self.removed,
                on_chunk = lambda n: (
                    tracker.add_bytes(n),
                    self.webhook.copy_progress(
                        self.camera, tracker.done_files, total_files,
                        tracker.done_bytes, total_bytes, tracker.eta_seconds(),
                    ),
                )[-1],
            )
        except CardRemovedError:
            raise
        except HashMismatchError as exc:
            log.error(f"  HASH MISMATCH {rel}: {exc}")
            self.webhook.copy_failed(self.camera, str(exc))
            with counts_lock: counts["failed"] += 1
            tracker.file_done()
            return
        except OSError as exc:
            log.error(f"  FAIL {rel}: {exc}")
            with counts_lock: counts["failed"] += 1
            tracker.file_done()
            return

        # -- record success ----------------------------------------------------
        self.state.mark_copied(rel, entry.size, result.source_hash, dst)
        # Flush after every file: power loss never loses more than one file.
        save_state(self.config.state_dir, self.state)

        with counts_lock: counts["copied"] += 1
        tracker.file_done()
        self.webhook.copy_progress(
            self.camera, tracker.done_files, total_files,
            tracker.done_bytes, total_bytes, tracker.eta_seconds(),
        )


# -----------------------------------------------------------------------------
# Top-level orchestration
# -----------------------------------------------------------------------------

class CopyOrchestrator:
    def __init__(
        self,
        mount_point: Path,
        device:      str,
        config:      Config,
        state:       CardState,
        webhook:     WebhookSender,
    ) -> None:
        self.mount_point   = mount_point
        self.device        = device
        self.config        = config
        self.state         = state
        self.webhook       = webhook
        self._detector     = RemovalDetector(device, mount_point)
        self._all_complete = False
        self._files_copied = 0   # total files newly written this session

    def run(self) -> None:
        self._detector.start()
        try:
            self._orchestrate()
        except CardRemovedError as exc:
            log.error(f"Copy aborted — card removed: {exc}")
            self.webhook.copy_failed("*", str(exc))
        except Exception as exc:
            log.error(f"Unexpected error: {exc}", exc_info=True)
            self.webhook.copy_failed("*", f"Unexpected error: {exc}")
        finally:
            self._detector.stop()
            save_state(self.config.state_dir, self.state)
            log.info("State saved.")
            self.webhook.card_removed(copy_completed=self._all_complete)
            if self._files_copied > 0:
                self._unmount()
            else:
                log.info(
                    "No files were copied this session — "
                    "leaving card mounted."
                )

    def _unmount(self) -> None:
        """
        Lazy-unmount the card and remove the mount point directory.
        Only called when at least one file was copied this session, so
        a card inserted for browsing or with no matching config is left
        mounted for the user to access normally.
        """
        try:
            mounted = any(
                str(self.mount_point) in line
                for line in Path("/proc/mounts").read_text().splitlines()
            )
        except OSError:
            mounted = True  # assume mounted if /proc/mounts is unreadable

        if not mounted:
            log.info(f"{self.mount_point} already unmounted.")
        else:
            try:
                subprocess.run(
                    ["umount", "-l", str(self.mount_point)],
                    check=True, capture_output=True,
                )
                log.info(f"Unmounted {self.mount_point}.")
            except subprocess.CalledProcessError as exc:
                log.warning(
                    f"umount failed for {self.mount_point}: "
                    f"{exc.stderr.decode().strip()}"
                )

        try:
            self.mount_point.rmdir()
            log.debug(f"Removed mount point directory {self.mount_point}.")
        except OSError:
            pass  # non-empty or already gone — not an error

    def _orchestrate(self) -> None:
        # -- batch EXIF scan ---------------------------------------------------
        log.info("Scanning card with exiftool...")
        all_paths = [
            p for p in self.mount_point.rglob("*")
            if p.is_file()
            and not p.name.startswith(".")
            and p.suffix.lower() != ".part"
        ]
        exif_map = scan_exif_batch(all_paths)
        exif_hits = sum(1 for e in exif_map.values() if e.camera_type)
        log.info(
            f"Scanned {len(all_paths)} files; "
            f"{exif_hits} identified by EXIF, "
            f"{len(all_paths) - exif_hits} by extension fallback."
        )

        # -- bucket by camera type, then derive camera lists -------------------
        buckets = collect_all_files(self.mount_point, exif_map)

        configured, unconfigured = cameras_from_buckets(buckets, self.config.cameras)

        if unconfigured:
            log.info(f"Detected but not configured (skipped): {unconfigured}")

        # Fire card_inserted with everything found, not just configured cameras
        self.webhook.card_inserted(configured + unconfigured)

        if not configured:
            log.warning("No detected cameras have a config entry — nothing to do.")
            return

        # -- build jobs --------------------------------------------------------
        jobs: list[CameraJob] = []
        for camera in configured:
            entries = buckets.get(camera, [])
            if not entries:
                log.info(f"[{camera}] No files on card for this camera type.")
                continue
            jobs.append(CameraJob(
                camera      = camera,
                cam_cfg     = self.config.cameras[camera],
                entries     = entries,
                mount_point = self.mount_point,
                card_uuid   = self.state.card_uuid,
                card_label  = self.state.card_label,
                config      = self.config,
                state       = self.state,
                webhook     = self.webhook,
                removed     = self._detector.removed,
            ))

        if not jobs:
            log.info("No files to copy for any configured camera.")
            return

        # -- run cameras in parallel -------------------------------------------
        n_cam_workers = min(self.config.max_parallel_cameras, len(jobs))
        log.info(
            f"Starting {len(jobs)} camera job(s), "
            f"{n_cam_workers} camera(s) in parallel."
        )

        all_ok = True
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=n_cam_workers,
            thread_name_prefix="camera",
        ) as pool:
            futures = {pool.submit(job.run): job for job in jobs}
            for fut in concurrent.futures.as_completed(futures):
                if self._detector.removed.is_set():
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise CardRemovedError(
                        "Card removed during parallel camera copy"
                    )
                try:
                    ok, n_copied = fut.result()
                    if not ok:
                        all_ok = False
                    self._files_copied += n_copied
                except CardRemovedError:
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as exc:
                    log.error(f"Camera job raised: {exc}", exc_info=True)
                    all_ok = False

        self._all_complete = all_ok


# -----------------------------------------------------------------------------
# Camera list derived from EXIF scan (replaces detect-camera-content.sh)
# -----------------------------------------------------------------------------

def cameras_from_buckets(
    buckets:    dict[str, list[FileEntry]],
    configured: dict[str, CameraConfig],
) -> tuple[list[str], list[str]]:
    """
    Derive detected camera types directly from the EXIF/extension scan.

    Returns (configured_cameras, unconfigured_cameras) where:
      - configured_cameras: present on card AND have a config entry,
        ordered to match the config file declaration order.
      - unconfigured_cameras: present on card but no config entry,
        sorted alphabetically.

    The "Unknown" bucket (files that matched neither EXIF nor extension
    fallback) is excluded from both lists.
    """
    present      = set(buckets.keys()) - {"Unknown"}
    configured_  = [c for c in configured if c in present]
    unconfigured = sorted(present - set(configured_))
    return configured_, unconfigured


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Automatic SD card copy with EXIF routing, "
            "chunk hashing, and parallel copies."
        )
    )
    parser.add_argument("mount_point", type=Path)
    parser.add_argument("kernel_dev")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    mount_point: Path = args.mount_point
    kernel_dev:  str  = args.kernel_dev
    device:      str  = f"/dev/{kernel_dev}"

    log.info("=" * 60)
    log.info(f"sdcard-copy start  device={device}  mount={mount_point}")

    try:
        config = load_config(args.config)
    except (FileNotFoundError, ValueError) as exc:
        log.error(f"Config error: {exc}")
        sys.exit(1)

    if config.log_path != DEFAULT_LOG:
        for h in log.handlers[:]:
            if isinstance(h, logging.FileHandler):
                log.removeHandler(h)
        fh = logging.FileHandler(config.log_path)
        fh.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)-8s] %(message)s")
        )
        fh.setLevel(logging.DEBUG)
        log.addHandler(fh)

    config.state_dir.mkdir(parents=True, exist_ok=True)

    card_uuid, card_label = get_card_identity(device)
    log.info(f"Card UUID={card_uuid!r}  LABEL={card_label!r}")

    state = load_state(config.state_dir, card_uuid, card_label, device)

    base_payload = {
        "card_uuid"  : card_uuid,
        "card_label" : card_label,
        "device"     : device,
        "mount_point": str(mount_point),
        "kernel_dev" : kernel_dev,
    }
    webhook = WebhookSender(config.webhooks, base_payload)

    CopyOrchestrator(mount_point, device, config, state, webhook).run()

    log.info("sdcard-copy finished.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
