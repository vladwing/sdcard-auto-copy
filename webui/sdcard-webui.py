#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "flask>=3.0",
# ]
# ///
"""
sdcard-webui.py — Live progress monitor for sdcard-copy.py

Receives HTTP webhooks from sdcard-copy.py and pushes real-time updates
to any connected browser via Server-Sent Events.

Usage:
    uv run sdcard-webui.py [--host 0.0.0.0] [--port 7777]

Point sdcard-copy.py's webhook URLs at this server, e.g.:
    card_inserted  = "http://kodiak.local:7777/webhook"
    copy_started   = "http://kodiak.local:7777/webhook"
    copy_progress  = "http://kodiak.local:7777/webhook"
    copy_finished  = "http://kodiak.local:7777/webhook"
    copy_failed    = "http://kodiak.local:7777/webhook"
    card_removed   = "http://kodiak.local:7777/webhook"

All six event types go to the same /webhook endpoint — the 'event' field
in the JSON body is used to dispatch state updates.
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from flask import Flask, Response, request, jsonify


# ---------------------------------------------------------------------------
# State model
# ---------------------------------------------------------------------------

@dataclass
class CameraState:
    name:           str
    status:         str = "waiting"       # waiting | copying | finished | failed
    total_files:    int = 0
    total_bytes:    int = 0
    files_done:     int = 0
    bytes_done:     int = 0
    percent:        float = 0.0
    eta_seconds:    Optional[int] = None
    files_copied:   int = 0
    files_skipped:  int = 0
    files_errored:  int = 0
    files_deleted:  int = 0
    error:          Optional[str] = None
    started_at:     Optional[str] = None
    finished_at:    Optional[str] = None


@dataclass
class CardState:
    card_uuid:   str
    card_label:  str
    device:      str
    mount_point: str
    kernel_dev:  str
    status:      str = "inserted"        # inserted | copying | finished | failed | removed
    cameras:     dict[str, CameraState] = field(default_factory=dict)
    inserted_at: str = field(default_factory=lambda: datetime.now().isoformat())
    removed_at:  Optional[str] = None
    copy_completed: bool = False


# Active cards: card_uuid -> CardState
# Removed cards moved to history (capped at 20)
_state_lock  = threading.Lock()
_active:  dict[str, CardState] = {}
_history: list[CardState]      = []
_MAX_HISTORY = 20


def _now() -> str:
    return datetime.now().isoformat()


def _handle_event(payload: dict) -> None:
    """Dispatch an incoming webhook payload to the appropriate state update."""
    event      = payload.get("event", "")
    card_uuid  = payload.get("card_uuid", "unknown")
    card_label = payload.get("card_label", "")
    device     = payload.get("device", "")
    mount_point= payload.get("mount_point", "")
    kernel_dev = payload.get("kernel_dev", "")

    with _state_lock:
        # Ensure card exists for all events except the very first
        if card_uuid not in _active:
            _active[card_uuid] = CardState(
                card_uuid   = card_uuid,
                card_label  = card_label,
                device      = device,
                mount_point = mount_point,
                kernel_dev  = kernel_dev,
            )
        card = _active[card_uuid]

        if event == "card_inserted":
            card.status      = "inserted"
            card.card_label  = card_label
            card.device      = device
            card.mount_point = mount_point
            card.inserted_at = _now()
            # Pre-populate camera slots so they appear immediately
            for cam in payload.get("cameras_detected", []):
                if cam not in card.cameras:
                    card.cameras[cam] = CameraState(name=cam)

        elif event == "copy_started":
            cam_name = payload.get("camera", "")
            card.status = "copying"
            if cam_name not in card.cameras:
                card.cameras[cam_name] = CameraState(name=cam_name)
            cam = card.cameras[cam_name]
            cam.status      = "copying"
            cam.total_files = payload.get("total_files", 0)
            cam.total_bytes = payload.get("total_bytes", 0)
            cam.started_at  = _now()

        elif event == "copy_progress":
            cam_name = payload.get("camera", "")
            if cam_name not in card.cameras:
                card.cameras[cam_name] = CameraState(name=cam_name)
            cam = card.cameras[cam_name]
            cam.status      = "copying"
            cam.files_done  = payload.get("files_done",  cam.files_done)
            cam.total_files = payload.get("files_total", cam.total_files)
            cam.bytes_done  = payload.get("bytes_done",  cam.bytes_done)
            cam.total_bytes = payload.get("bytes_total", cam.total_bytes)
            cam.percent     = payload.get("percent",     cam.percent)
            cam.eta_seconds = payload.get("eta_seconds")

        elif event == "copy_finished":
            cam_name = payload.get("camera", "")
            if cam_name not in card.cameras:
                card.cameras[cam_name] = CameraState(name=cam_name)
            cam = card.cameras[cam_name]
            cam.status         = "finished"
            cam.percent        = 100.0
            cam.files_copied   = payload.get("files_copied",  0)
            cam.files_skipped  = payload.get("files_skipped", 0)
            cam.files_errored  = payload.get("files_errored", 0)
            cam.files_deleted  = payload.get("files_deleted", 0)
            cam.bytes_done     = payload.get("bytes_total",   cam.total_bytes)
            cam.eta_seconds    = 0
            cam.finished_at    = _now()
            # Promote to failed if any files errored
            if cam.files_errored > 0:
                cam.status = "failed"
            # If all cameras are done, mark card finished or failed
            if all(c.status in ("finished", "failed")
                   for c in card.cameras.values()):
                card.status = (
                    "failed"
                    if any(c.status == "failed" for c in card.cameras.values())
                    else "finished"
                )

        elif event == "copy_failed":
            cam_name = payload.get("camera", "*")
            if cam_name == "*":
                card.status = "failed"
                for c in card.cameras.values():
                    if c.status == "copying":
                        c.status = "failed"
                        c.error  = payload.get("reason", "Unknown error")
            else:
                if cam_name not in card.cameras:
                    card.cameras[cam_name] = CameraState(name=cam_name)
                cam = card.cameras[cam_name]
                cam.status = "failed"
                cam.error  = payload.get("reason", "Unknown error")

        elif event == "card_removed":
            card.status         = "removed"
            card.removed_at     = _now()
            card.copy_completed = payload.get("copy_completed", False)
            # Move to history
            _active.pop(card_uuid, None)
            _history.insert(0, card)
            if len(_history) > _MAX_HISTORY:
                _history.pop()


# ---------------------------------------------------------------------------
# Server-Sent Events broker
# ---------------------------------------------------------------------------

class SSEBroker:
    """
    Fan-out broker: every connected browser client gets its own queue.
    When state changes, _push() serialises the full current state and
    enqueues it for all connected clients.
    """

    def __init__(self) -> None:
        self._clients: set[queue.SimpleQueue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.SimpleQueue:
        q: queue.SimpleQueue = queue.SimpleQueue()
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q: queue.SimpleQueue) -> None:
        with self._lock:
            self._clients.discard(q)

    def push(self) -> None:
        """Serialise current state and send to all clients."""
        with _state_lock:
            payload = {
                "active":  [asdict(c) for c in _active.values()],
                "history": [asdict(c) for c in _history],
                "ts":      _now(),
            }
        data = json.dumps(payload)
        msg  = f"data: {data}\n\n"
        with self._lock:
            dead = set()
            for q in self._clients:
                try:
                    q.put_nowait(msg)
                except Exception:
                    dead.add(q)
            self._clients -= dead


broker = SSEBroker()


# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------

app = Flask(__name__)
app.config["SECRET_KEY"] = "sdcard-monitor-not-secret"


@app.post("/webhook")
def webhook():
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "invalid JSON"}), 400
    try:
        _handle_event(payload)
    except Exception as exc:
        app.logger.error(f"Error handling event: {exc}", exc_info=True)
        return jsonify({"error": str(exc)}), 500
    broker.push()
    return jsonify({"ok": True})


@app.get("/events")
def events():
    def stream(q: queue.SimpleQueue):
        # Send full state immediately on connect
        broker.push()
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield msg
                except queue.Empty:
                    # Keepalive comment so proxies don't close the connection
                    yield ": keepalive\n\n"
        except GeneratorExit:
            pass
        finally:
            broker.unsubscribe(q)

    q = broker.subscribe()
    return Response(
        stream(q),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":  "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )


@app.get("/")
def index():
    return Response(_HTML, mimetype="text/html")


# ---------------------------------------------------------------------------
# Embedded HTML/CSS/JS  (single-file requirement)
# ---------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SD Card Monitor</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@300;400;500;600&family=Space+Grotesk:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  /* ── Reset & base ─────────────────────────────────────────────────────── */
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:          #0e0f11;
    --bg-card:     #151618;
    --bg-elevated: #1c1e21;
    --border:      #2a2d32;
    --border-hi:   #3d4148;
    --text:        #d4d8de;
    --text-dim:    #6b7280;
    --text-faint:  #3d4148;
    --amber:       #f59e0b;
    --amber-dim:   #78450a;
    --amber-glow:  rgba(245,158,11,0.08);
    --green:       #22c55e;
    --green-dim:   #14532d;
    --green-glow:  rgba(34,197,94,0.08);
    --red:         #ef4444;
    --red-dim:     #7f1d1d;
    --red-glow:    rgba(239,68,68,0.08);
    --blue:        #60a5fa;
    --blue-dim:    #1e3a5f;
    --blue-glow:   rgba(96,165,250,0.08);
    --orange:      #fb923c;
    --orange-dim:  #7c2d12;
    --orange-glow: rgba(251,146,60,0.08);
    --mono:        'IBM Plex Mono', monospace;
    --sans:        'Space Grotesk', sans-serif;
    --r:           6px;
  }

  html { height: 100%; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    font-size: 14px;
    min-height: 100%;
    line-height: 1.5;
  }

  /* ── Layout ───────────────────────────────────────────────────────────── */
  .layout {
    max-width: 1120px;
    margin: 0 auto;
    padding: 0 24px 64px;
  }

  /* ── Header ───────────────────────────────────────────────────────────── */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 28px 0 24px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 32px;
  }

  .logo {
    display: flex;
    align-items: center;
    gap: 12px;
  }

  .logo-icon {
    width: 36px; height: 36px;
    border: 1.5px solid var(--amber);
    border-radius: var(--r);
    display: flex; align-items: center; justify-content: center;
    color: var(--amber);
    font-family: var(--mono);
    font-size: 16px;
    font-weight: 600;
    box-shadow: 0 0 12px var(--amber-dim);
  }

  .logo-text {
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--text);
  }

  .logo-sub {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
    letter-spacing: 0.08em;
    margin-top: 1px;
  }

  .header-status {
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
  }

  .dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--text-faint);
    transition: background 0.4s, box-shadow 0.4s;
  }
  .dot.live {
    background: var(--green);
    box-shadow: 0 0 6px var(--green);
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
  }

  /* ── Section headers ──────────────────────────────────────────────────── */
  .section-label {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--text-faint);
    margin-bottom: 16px;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .section-label::after {
    content: '';
    flex: 1;
    height: 1px;
    background: var(--border);
  }

  /* ── Empty state ──────────────────────────────────────────────────────── */
  .empty {
    border: 1px dashed var(--border);
    border-radius: var(--r);
    padding: 48px 24px;
    text-align: center;
    color: var(--text-dim);
  }
  .empty-icon {
    font-size: 32px;
    margin-bottom: 12px;
    opacity: 0.4;
  }
  .empty p { font-size: 13px; }
  .empty code {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
    background: var(--bg-elevated);
    padding: 2px 6px;
    border-radius: 3px;
  }

  /* ── Card panel ───────────────────────────────────────────────────────── */
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: var(--r);
    margin-bottom: 16px;
    overflow: hidden;
    transition: border-color 0.3s;
  }
  .card.status-copying  { border-color: var(--amber-dim); }
  .card.status-finished { border-color: var(--green-dim); }
  .card.status-failed   { border-color: var(--red-dim);   }
  .card.status-removed  {
    opacity: 0.55;
    filter: saturate(0.4);
  }

  .card-header {
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: center;
    gap: 16px;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
  }

  .card-icon {
    font-size: 20px;
    line-height: 1;
  }

  .card-meta {
    min-width: 0;
  }

  .card-title {
    font-weight: 600;
    font-size: 14px;
    color: var(--text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .card-sub {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--text-dim);
    margin-top: 2px;
    letter-spacing: 0.04em;
  }

  /* ── Status badge ─────────────────────────────────────────────────────── */
  .badge {
    font-family: var(--mono);
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    padding: 3px 8px;
    border-radius: 3px;
    white-space: nowrap;
  }
  .badge-inserted { background: var(--bg-elevated); color: var(--text-dim); border: 1px solid var(--border-hi); }
  .badge-copying  { background: var(--amber-glow);  color: var(--amber);    border: 1px solid var(--amber-dim); }
  .badge-finished { background: var(--green-glow);  color: var(--green);    border: 1px solid var(--green-dim); }
  .badge-failed   { background: var(--red-glow);    color: var(--red);      border: 1px solid var(--red-dim);   }
  .badge-removed  { background: var(--bg-elevated); color: var(--text-faint); border: 1px solid var(--border);  }

  /* ── Camera rows ──────────────────────────────────────────────────────── */
  .cameras {
    padding: 12px 20px 16px;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }

  .cam-row {}

  .cam-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 6px;
    gap: 8px;
  }

  .cam-name {
    font-weight: 500;
    font-size: 13px;
    color: var(--text);
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .cam-right {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-shrink: 0;
  }

  .cam-stats {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--text-dim);
    text-align: right;
  }

  .cam-eta {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--amber);
    min-width: 52px;
    text-align: right;
  }
  .cam-eta.done { color: var(--green); }

  /* ── Progress bar ─────────────────────────────────────────────────────── */
  .bar-track {
    height: 4px;
    background: var(--bg-elevated);
    border-radius: 2px;
    overflow: hidden;
  }

  .bar-fill {
    height: 100%;
    border-radius: 2px;
    transition: width 0.6s ease, background 0.4s;
    background: var(--amber);
    min-width: 2px;
  }
  .bar-fill.done    { background: var(--green); }
  .bar-fill.failed  { background: var(--red);   }
  .bar-fill.waiting { background: var(--border-hi); }

  /* ── Cam error ────────────────────────────────────────────────────────── */
  .cam-error {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--red);
    margin-top: 5px;
    padding: 4px 8px;
    background: var(--red-glow);
    border-radius: 3px;
    border-left: 2px solid var(--red);
    word-break: break-all;
  }

  /* ── Outcome line ─────────────────────────────────────────────────────── */
  .outcome {
    display: flex;
    align-items: center;
    gap: 6px;
    font-family: var(--mono);
    font-size: 10px;
    padding: 5px 20px 0;
  }
  .outcome-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .outcome-ok   .outcome-dot { background: var(--green); }
  .outcome-fail .outcome-dot { background: var(--red);   }
  .outcome-removed .outcome-dot { background: var(--text-dim); }
  .outcome-ok   { color: var(--green);    }
  .outcome-fail { color: var(--red);      }
  .outcome-removed { color: var(--text-dim); }

  /* ── Card footer ──────────────────────────────────────────────────────── */
  .card-footer {
    border-top: 1px solid var(--border);
    padding: 8px 20px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }

  .footer-pills {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  .pill {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--text-dim);
    display: flex;
    align-items: center;
    gap: 4px;
  }
  .pill span { color: var(--text-faint); }

  /* Stat pill colour variants */
  .pill-copied   { color: var(--green);  }
  .pill-skipped  { color: var(--text-dim); }
  .pill-errored  { color: var(--red);    }
  .pill-deleted  { color: var(--orange); }
  .pill-bytes    { color: var(--blue);   }
  .pill-copied  span,
  .pill-skipped span,
  .pill-errored span,
  .pill-deleted span,
  .pill-bytes   span { color: inherit; opacity: 0.6; }

  .removed-time {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--text-faint);
  }

  /* ── History section ──────────────────────────────────────────────────── */
  #history-section { margin-top: 40px; }

  /* ── Responsive ───────────────────────────────────────────────────────── */
  @media (max-width: 600px) {
    .cam-right { gap: 6px; }
    .cam-stats { display: none; }
    .footer-pills { gap: 6px; }
  }

  /* ── Animations ───────────────────────────────────────────────────────── */
  @keyframes slideIn {
    from { opacity: 0; transform: translateY(-8px); }
    to   { opacity: 1; transform: translateY(0); }
  }
  .card { animation: slideIn 0.25s ease; }
</style>
</head>
<body>
<div class="layout">

  <header>
    <div class="logo">
      <div class="logo-icon">SD</div>
      <div>
        <div class="logo-text">Card Monitor</div>
        <div class="logo-sub">sdcard-copy.py · live</div>
      </div>
    </div>
    <div class="header-status">
      <div class="dot" id="status-dot"></div>
      <span id="status-text">connecting…</span>
    </div>
  </header>

  <div id="active-section">
    <div class="section-label">Active</div>
    <div id="active-cards">
      <div class="empty" id="empty-active">
        <div class="empty-icon">⏳</div>
        <p>Waiting for cards…</p>
        <p style="margin-top:8px">Insert a card or check that webhooks point to
          <code>http://&lt;this-host&gt;:7777/webhook</code></p>
      </div>
    </div>
  </div>

  <div id="history-section" style="display:none">
    <div class="section-label">History</div>
    <div id="history-cards"></div>
  </div>

</div>

<script>
const $ = id => document.getElementById(id);

// ── SSE connection ──────────────────────────────────────────────────────────
let es;
function connect() {
  es = new EventSource('/events');
  es.onopen = () => {
    $('status-dot').classList.add('live');
    $('status-text').textContent = 'live';
  };
  es.onmessage = e => {
    try { reconcile(JSON.parse(e.data)); }
    catch(err) { console.error('parse error', err); }
  };
  es.onerror = () => {
    $('status-dot').classList.remove('live');
    $('status-text').textContent = 'reconnecting…';
    es.close();
    setTimeout(connect, 3000);
  };
}
connect();

// ── Helpers ─────────────────────────────────────────────────────────────────
function fmt_bytes(b) {
  if (b == null || b === 0) return '—';
  if (b < 1024) return b + ' B';
  if (b < 1024**2) return (b/1024).toFixed(1) + ' KB';
  if (b < 1024**3) return (b/1024**2).toFixed(1) + ' MB';
  return (b/1024**3).toFixed(2) + ' GB';
}
function fmt_eta(s) {
  if (s == null) return '';
  if (s === 0)   return 'done';
  if (s < 60)    return s + 's';
  if (s < 3600)  return Math.floor(s/60) + 'm ' + (s%60) + 's';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}
function fmt_time(iso) {
  if (!iso) return '';
  return new Date(iso).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
}
function card_emoji(status) {
  return { inserted:'💾', copying:'📋', finished:'✅', failed:'❌', removed:'⏏️' }[status] || '💾';
}
// Only update a text node if the value actually changed
function set_text(el, val) {
  if (el && el.textContent !== val) el.textContent = val;
}
// Only update an attribute if the value actually changed
function set_attr(el, attr, val) {
  if (el && el.getAttribute(attr) !== val) el.setAttribute(attr, val);
}
// Toggle a CSS class without touching others
function set_class(el, cls, on) {
  if (!el) return;
  if (on) el.classList.add(cls); else el.classList.remove(cls);
}

// ── Build HTML for a brand-new camera row ───────────────────────────────────
// data-* attributes are the stable hooks used by patch_camera() below.
function build_camera_html(cam) {
  return `
  <div class="cam-row" data-cam="${cam.name}">
    <div class="cam-header">
      <div class="cam-name">${cam.name}</div>
      <div class="cam-right">
        <div class="cam-stats" data-cam-stats></div>
        <div class="cam-eta"   data-cam-eta></div>
      </div>
    </div>
    <div class="bar-track">
      <div class="bar-fill" data-cam-bar style="width:0%"></div>
    </div>
    <div class="cam-error" data-cam-error style="display:none"></div>
    <div class="footer-pills" data-cam-pills style="margin-top:5px;display:none"></div>
  </div>`;
}

// ── Patch an existing camera row in-place ───────────────────────────────────
function patch_camera(row, cam) {
  const is_done = cam.status === 'finished';
  const is_fail = cam.status === 'failed';
  const is_wait = cam.status === 'waiting';
  const pct     = is_done ? 100 : (cam.percent ?? 0);

  const bar = row.querySelector('[data-cam-bar]');
  if (bar) {
    const w = pct + '%';
    if (bar.style.width !== w) bar.style.width = w;
    set_class(bar, 'done',    is_done);
    set_class(bar, 'failed',  is_fail);
    set_class(bar, 'waiting', is_wait);
  }

  const statsEl = row.querySelector('[data-cam-stats]');
  set_text(statsEl, is_wait ? '—' : `${cam.files_done}/${cam.total_files} · ${fmt_bytes(cam.bytes_done)}`);

  const etaEl = row.querySelector('[data-cam-eta]');
  const eta_text = is_done
    ? (cam.files_skipped === cam.total_files && cam.files_copied === 0 ? 'skipped' : 'done')
    : fmt_eta(cam.eta_seconds);
  set_text(etaEl, eta_text);
  set_class(etaEl, 'done', is_done);

  const errEl = row.querySelector('[data-cam-error]');
  if (errEl) {
    if (cam.error) {
      errEl.style.display = '';
      set_text(errEl, '⚠ ' + cam.error);
    } else {
      errEl.style.display = 'none';
    }
  }

  const pillsEl = row.querySelector('[data-cam-pills]');
  if (pillsEl) {
    const parts = [];
    if (cam.files_copied  > 0) parts.push(`<span class="pill pill-copied"><span>copied</span> ${cam.files_copied}</span>`);
    if (cam.files_skipped > 0) parts.push(`<span class="pill pill-skipped"><span>skipped</span> ${cam.files_skipped}</span>`);
    if (cam.files_errored > 0) parts.push(`<span class="pill pill-errored"><span>errored</span> ${cam.files_errored}</span>`);
    if (cam.files_deleted > 0) parts.push(`<span class="pill pill-deleted"><span>deleted</span> ${cam.files_deleted}</span>`);
    const html = parts.join('');
    if (pillsEl.innerHTML !== html) pillsEl.innerHTML = html;
    pillsEl.style.display = html ? '' : 'none';
  }
}

// ── Compute a card's job outcome ────────────────────────────────────────────
// Returns {cls, text} describing the final state once a card is done/removed.
function card_outcome(c) {
  if (c.status === 'removed') {
    const all_cams = Object.values(c.cameras);
    if (all_cams.length === 0) return { cls: 'outcome-removed', text: 'Card removed' };
    const all_done = all_cams.every(x => x.status === 'finished' || x.status === 'failed');
    if (!all_done) return { cls: 'outcome-removed', text: 'Card removed before copy finished' };
    const any_err  = all_cams.some(x => x.files_errored > 0);
    if (any_err)   return { cls: 'outcome-fail',    text: 'Card removed — copy finished with errors' };
    return { cls: 'outcome-ok', text: 'Card removed — copy finished successfully' };
  }
  if (c.status === 'finished') {
    const any_err = Object.values(c.cameras).some(x => x.files_errored > 0);
    if (any_err)  return { cls: 'outcome-fail', text: 'Finished with errors' };
    return { cls: 'outcome-ok', text: 'Finished successfully' };
  }
  if (c.status === 'failed') return { cls: 'outcome-fail', text: 'Copy failed' };
  return null;   // still in progress — don't show outcome line
}

// ── Build HTML for a brand-new card element ─────────────────────────────────
function build_card_html(c, is_history) {
  const label    = c.card_label || 'Unlabeled';
  const sub_info = [c.device, c.card_uuid.slice(0,13)].filter(Boolean).join(' · ');
  const ts       = (c.removed_at && is_history) ? c.removed_at : c.inserted_at;
  return `
  <div class="card status-${c.status}" data-uuid="${c.card_uuid}">
    <div class="card-header">
      <div class="card-icon" data-card-icon>${card_emoji(c.status)}</div>
      <div class="card-meta">
        <div class="card-title">${label}</div>
        <div class="card-sub">${sub_info}</div>
      </div>
      <span class="badge badge-${c.status}" data-card-badge>${{inserted:'Inserted',copying:'Copying',finished:'Done',failed:'Failed',removed:'Removed'}[c.status]||c.status}</span>
    </div>
    <div class="cameras" data-card-cameras>
      ${Object.values(c.cameras).length
          ? Object.values(c.cameras).map(build_camera_html).join('')
          : `<div style="font-family:var(--mono);font-size:11px;color:var(--text-dim);padding:4px 0">No cameras detected yet…</div>`}
    </div>
    <div class="outcome" data-card-outcome></div>
    <div class="card-footer">
      <div class="footer-pills" data-card-footer-pills>
        <span class="pill"><span>device</span> ${c.device}</span>
      </div>
      <span class="removed-time" data-card-time>${fmt_time(ts)}</span>
    </div>
  </div>`;
}

// ── Patch an existing card element in-place ──────────────────────────────────
function patch_card(el, c, is_history) {
  // Status class on the card wrapper
  const prev_status = [...el.classList].find(k => k.startsWith('status-'));
  const next_status = 'status-' + c.status;
  if (prev_status !== next_status) {
    if (prev_status) el.classList.remove(prev_status);
    el.classList.add(next_status);
  }

  // Icon + badge
  set_text(el.querySelector('[data-card-icon]'), card_emoji(c.status));
  const badgeEl = el.querySelector('[data-card-badge]');
  if (badgeEl) {
    const label = {inserted:'Inserted',copying:'Copying',finished:'Done',failed:'Failed',removed:'Removed'}[c.status]||c.status;
    set_text(badgeEl, label);
    // Replace badge-* class
    const prev = [...badgeEl.classList].find(k => k.startsWith('badge-') && k !== 'badge');
    if (prev !== 'badge-' + c.status) {
      if (prev) badgeEl.classList.remove(prev);
      badgeEl.classList.add('badge-' + c.status);
    }
  }

  // Camera rows — add new ones, patch existing ones
  const camsEl = el.querySelector('[data-card-cameras]');
  if (camsEl) {
    const cam_data = Object.values(c.cameras);
    if (cam_data.length === 0) return;

    // Remove the "no cameras" placeholder if present
    const placeholder = camsEl.querySelector('div:not(.cam-row)');
    if (placeholder) camsEl.innerHTML = '';

    for (const cam of cam_data) {
      let row = camsEl.querySelector(`[data-cam="${CSS.escape(cam.name)}"]`);
      if (!row) {
        camsEl.insertAdjacentHTML('beforeend', build_camera_html(cam));
        row = camsEl.querySelector(`[data-cam="${CSS.escape(cam.name)}"]`);
      }
      patch_camera(row, cam);
    }
  }

  // Footer aggregate stats pills
  const pillsEl = el.querySelector('[data-card-footer-pills]');
  if (pillsEl) {
    const total_copied  = Object.values(c.cameras).reduce((a,x) => a + (x.files_copied||0),  0);
    const total_skipped = Object.values(c.cameras).reduce((a,x) => a + (x.files_skipped||0), 0);
    const total_errored = Object.values(c.cameras).reduce((a,x) => a + (x.files_errored||0), 0);
    const total_deleted = Object.values(c.cameras).reduce((a,x) => a + (x.files_deleted||0), 0);
    const total_bytes   = Object.values(c.cameras).reduce((a,x) => a + (x.bytes_done||0),    0);
    const parts = [`<span class="pill"><span>device</span> ${c.device}</span>`];
    if (total_copied)  parts.push(`<span class="pill pill-copied"><span>copied</span> ${total_copied}</span>`);
    if (total_skipped) parts.push(`<span class="pill pill-skipped"><span>skipped</span> ${total_skipped}</span>`);
    if (total_errored) parts.push(`<span class="pill pill-errored"><span>errored</span> ${total_errored}</span>`);
    if (total_deleted) parts.push(`<span class="pill pill-deleted"><span>deleted</span> ${total_deleted}</span>`);
    if (total_bytes)   parts.push(`<span class="pill pill-bytes"><span>total</span> ${fmt_bytes(total_bytes)}</span>`);
    const html = parts.join('');
    if (pillsEl.innerHTML !== html) pillsEl.innerHTML = html;
  }

  // Outcome line (shown once card reaches a terminal state)
  const outcomeEl = el.querySelector('[data-card-outcome]');
  if (outcomeEl) {
    const oc = card_outcome(c);
    if (oc) {
      const html = `<div class="outcome ${oc.cls}"><div class="outcome-dot"></div>${oc.text}</div>`;
      if (outcomeEl.innerHTML !== html) outcomeEl.innerHTML = html;
    } else {
      if (outcomeEl.innerHTML !== '') outcomeEl.innerHTML = '';
    }
  }

  // Timestamp
  const ts = (c.removed_at && is_history) ? c.removed_at : c.inserted_at;
  const timeEl = el.querySelector('[data-card-time]');
  set_text(timeEl, fmt_time(ts));
}

// ── Reconcile a container (active or history) ────────────────────────────────
// - Cards present in `cards` but not in the DOM  → inserted (with animation)
// - Cards present in both                        → patched in-place (no flicker)
// - Cards in the DOM but absent from `cards`     → removed
function reconcile_container(container, cards, is_history) {
  const by_uuid = Object.fromEntries(cards.map(c => [c.card_uuid, c]));

  // Remove stale cards
  for (const el of [...container.querySelectorAll('[data-uuid]')]) {
    if (!by_uuid[el.dataset.uuid]) el.remove();
  }

  // Insert or patch
  for (const c of cards) {
    let el = container.querySelector(`[data-uuid="${CSS.escape(c.card_uuid)}"]`);
    if (!el) {
      container.insertAdjacentHTML('beforeend', build_card_html(c, is_history));
    } else {
      patch_card(el, c, is_history);
    }
  }
}

// ── Top-level reconcile ──────────────────────────────────────────────────────
function reconcile(data) {
  const active  = data.active  || [];
  const history = data.history || [];

  const activeEl = $('active-cards');

  // Always remove the empty placeholder before reconciling — it will be
  // re-added below if there are genuinely no cards. This ensures stale card
  // elements left over from before a server restart are purged even when the
  // server comes back with an empty active list.
  const empty = activeEl.querySelector('#empty-active');
  if (empty) empty.remove();

  reconcile_container(activeEl, active, false);

  if (active.length === 0 && !activeEl.querySelector('[data-uuid]')) {
    activeEl.insertAdjacentHTML('beforeend', `
      <div class="empty" id="empty-active">
        <div class="empty-icon">⏳</div>
        <p>Waiting for cards…</p>
        <p style="margin-top:8px">Insert a card or check that webhooks point to
          <code>http://&lt;this-host&gt;:7777/webhook</code></p>
      </div>`);
  }

  const histSection = $('history-section');
  const histEl      = $('history-cards');
  if (history.length === 0) {
    histSection.style.display = 'none';
    // Clear any history cards left over from before a restart
    for (const el of [...histEl.querySelectorAll('[data-uuid]')] ) el.remove();
  } else {
    histSection.style.display = '';
    reconcile_container(histEl, history, true);
  }
}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SD card copy live monitor — receives webhooks, streams to browser."
    )
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=7777,
                        help="Bind port (default: 7777)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable Flask debug mode")
    args = parser.parse_args()

    print(f"SD Card Monitor  →  http://{'localhost' if args.host == '0.0.0.0' else args.host}:{args.port}")
    print(f"Webhook endpoint →  http://{'localhost' if args.host == '0.0.0.0' else args.host}:{args.port}/webhook")
    print()

    # Flask's dev server is fine here — this is a single-user LAN tool.
    # threaded=True is required so SSE streaming and webhook POSTs don't block each other.
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
