"""
gnome_ui.py

A deliberately tiny status page for the gnome bot: is HL:A connected right
now, where are TTS .wav files being saved (editable from the page itself),
which Twitch channel's chat to listen to, and a live log of what's been
played / commanded recently. (Chat is always listened to for !gnomevoice
and the special one-word commands like !chudly -- there's no "enable
chat" toggle; only the redeem endpoint ever triggers spoken TTS.)

Every user-editable field (output dir, Twitch channel) is persisted to a
small JSON file next to this script as soon as it changes, and reloaded
on startup -- so relaunching the bot keeps whatever was last set from the
page, instead of resetting to the hardcoded defaults every time.

The audio output directory has a "Browse..." button that pops a real,
native OS folder picker rather than a plain text box. Browser JS can't
give you a real filesystem path for security reasons (even <input
type="file" webkitdirectory"> only exposes a relative file list, never an
absolute path), so the trick is: the browser and this server are the same
machine, so the Python backend itself opens the dialog (via stdlib
tkinter, used purely as a way to summon the OS's native picker -- no Tk
window is ever actually shown) and hands the chosen path back over HTTP.
This only works when you're viewing the page on the same machine the bot
is running on; typing the path manually (still supported) is the fallback
for anything else (e.g. viewing the status page from your phone).

Design goal is low maintenance, not features:
  - stdlib only (http.server + threading + deque + tkinter). No Flask, no
    websockets, no build step, no extra pip installs.
  - One HTML page with a small inline <script> that polls a JSON endpoint
    once a second and patches the DOM. Polling instead of websockets means
    there's no persistent-connection lifecycle to manage or reconnect.
  - All state lives in a single UIState object the rest of the bot calls
    into (`ui_state.set_connected(...)`, `ui_state.log(...)`) -- the HTTP
    handler just reads it and serializes to JSON.

Usage from the bot:

    ui_state = UIState(output_dir=OUTPUT_DIR, twitch_channel="mychannel")
    hla_queries.start(retry_interval=1.0, on_status_change=ui_state.set_connected)
    ui_state.log("system", "Bot started")
    ...
    ui_state.log("tts", f"{username} -> voice #{voice_idx}: {message!r}")
    ...
    threading.Thread(target=run_ui_server, args=(ui_state,), daemon=True).start()

Then open http://127.0.0.1:8420/ in a browser.
"""

import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional


DEFAULT_UI_HOST = "127.0.0.1"
DEFAULT_UI_PORT = 8420

# Where persisted settings (output dir, twitch channel) are saved/loaded.
# Lives next to this script so it works regardless of the working
# directory the bot happens to be launched from.
DEFAULT_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gnome_bot_state.json")

# Only one native dialog can sensibly be open at a time (Tk isn't
# reliably safe to spin up concurrently from multiple threads), so
# concurrent browse requests are rejected rather than risking a crash.
_browse_lock = threading.Lock()


def _browse_for_directory(initial_dir: str) -> Optional[str]:
    """
    Opens a native OS folder-picker dialog and returns the chosen absolute
    path, or None if the user cancelled. Uses tkinter purely to summon the
    OS's own picker -- no actual Tk window is shown, it's created hidden
    and destroyed immediately after. Raises RuntimeError if a dialog can't
    be shown at all (no display available, already one open, etc.) so the
    caller can report a clear error and fall back to manual entry.
    """
    if not _browse_lock.acquire(blocking=False):
        raise RuntimeError("A folder browser is already open -- finish or cancel that one first")
    try:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError as e:
            raise RuntimeError(f"tkinter isn't available on this machine ({e})")

        try:
            root = tk.Tk()
        except Exception as e:
            # e.g. tkinter.TclError: no display -- this bot isn't running
            # on the same machine you're viewing the page from
            raise RuntimeError(f"couldn't open a native dialog here ({e})")

        try:
            root.withdraw()
            root.attributes("-topmost", True)
            chosen = filedialog.askdirectory(
                initialdir=initial_dir if os.path.isdir(initial_dir) else None,
                title="Select audio output directory",
                parent=root,
            )
        finally:
            root.destroy()
        return chosen or None
    finally:
        _browse_lock.release()


class UIState:
    """Thread-safe shared state the HTTP handler reads from."""

    def __init__(self, output_dir: str, twitch_channel: str = "", chat_enabled: bool = False,
                 state_file: str = DEFAULT_STATE_FILE, max_log_entries: int = 200):
        self.connected = False
        self._lock = threading.Lock()
        self._log = deque(maxlen=max_log_entries)
        self.state_file = state_file

        # These are just starting defaults -- _load() below overrides them
        # with whatever was last saved, if anything was.
        self.output_dir = output_dir
        self.twitch_channel = twitch_channel
        self.chat_enabled = chat_enabled
        self._load()

        # Registered by the bot (see UIState.set_test_handler / the
        # "Test" button's /api/test_tts endpoint) once it's ready to
        # actually generate/play a sample clip. Left as None until then
        # so a browse/test click during startup fails cleanly instead of
        # crashing.
        self._test_handler = None

    # ------------------------------------------------------------------ #
    # Persistence -- deliberately minimal: one JSON file, whole-state
    # overwrite on every change. No migrations, no partial updates.
    # ------------------------------------------------------------------ #
    def _load(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return  # no saved state yet (or it's corrupt) -- just keep defaults
        self.output_dir = saved.get("output_dir", self.output_dir)
        self.twitch_channel = saved.get("twitch_channel", self.twitch_channel)
        self.chat_enabled = bool(saved.get("chat_enabled", self.chat_enabled))

    def _persist(self):
        """Best-effort save of the fields that should survive a restart."""
        with self._lock:
            data = {
                "output_dir": self.output_dir,
                "twitch_channel": self.twitch_channel,
                "chat_enabled": self.chat_enabled,
            }
        try:
            tmp_path = self.state_file + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, self.state_file)  # atomic on the same filesystem
        except OSError as e:
            self.log("system", f"Warning: couldn't save settings to disk: {e}")

    # ------------------------------------------------------------------ #
    # Setters -- each updates state, logs it, and persists it
    # ------------------------------------------------------------------ #
    def set_connected(self, connected: bool):
        with self._lock:
            self.connected = connected
        self.log("system", "Connected to Half-Life: Alyx" if connected
                  else "Half-Life: Alyx not found -- retrying every second")
        # Not persisted: this reflects live game state, not a user setting.

    def set_chat_enabled(self, enabled: bool):
        with self._lock:
            self.chat_enabled = bool(enabled)
        self.log("system", "Chat TTS enabled -- normal chat messages will be spoken, in "
                  "addition to the redeem endpoint" if enabled else
                  "Chat TTS disabled -- only the redeem endpoint triggers TTS "
                  "(!gnomevoice and the special commands still work either way)")
        self._persist()

    # ------------------------------------------------------------------ #
    # "Test" button -- see /api/test_tts and set_test_handler()
    # ------------------------------------------------------------------ #
    def set_test_handler(self, handler):
        """Registers the function the "Test" button calls (no args, no
        return value expected). The bot calls this once, during startup,
        after everything TTS-related is ready to go."""
        self._test_handler = handler

    def request_test(self) -> bool:
        """Returns False (without raising) if no handler has been
        registered yet, e.g. the page was hit before the bot finished
        starting up."""
        handler = self._test_handler
        if handler is None:
            return False
        handler()
        return True

    def set_output_dir(self, path: str):
        """
        Changes where audio files get saved, from here on out. Raises
        ValueError/OSError on a bad path so the HTTP handler can report a
        useful error instead of silently accepting it -- doesn't touch
        self.output_dir until the new directory is confirmed usable.
        """
        path = path.strip()
        if not path:
            raise ValueError("Path cannot be empty")
        os.makedirs(path, exist_ok=True)  # raises OSError if not usable
        with self._lock:
            self.output_dir = path
        self.log("system", f"Audio output directory changed to: {path}")
        self._persist()

    def set_twitch_channel(self, channel: str):
        channel = channel.strip().lstrip("#")
        if not channel:
            raise ValueError("Twitch channel cannot be empty")
        with self._lock:
            self.twitch_channel = channel
        self.log("system", f"Twitch channel changed to: #{channel}")
        self._persist()

    def log(self, kind: str, text: str):
        """kind is a short tag for styling, e.g. 'tts', 'command', 'system'."""
        with self._lock:
            self._log.append({
                "ts": time.strftime("%H:%M:%S"),
                "kind": kind,
                "text": text,
            })

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "connected": self.connected,
                "output_dir": self.output_dir,
                "twitch_channel": self.twitch_channel,
                "chat_enabled": self.chat_enabled,
                "log": list(self._log)[::-1],  # newest first
            }


_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Gnome Bot Status</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=Syne:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #c3c3c3;
    --surface: #fff;
    --border: #2e2e38;
    --border2: #000;
    --accent: #008000;
    --hal-purp: #e165ee;
    --accent-glow: rgba(225,101,238,0.15);
    --text: #000;
    --text-dim: #555566;
    --error: #a32d2d;
    --info: #1d6fa5;
    --mono: 'DM Mono', monospace;
    --display: 'Syne', sans-serif;
  }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--display);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 3rem 1rem 2rem;
  }
  header { text-align: center; margin-bottom: 1rem; }
  header h1 {
    font-size: clamp(1.6rem, 5vw, 2.4rem);
    font-weight: 800;
    letter-spacing: -0.02em;
  }
  header p {
    margin-top: 0.5rem;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text-dim);
    letter-spacing: 0.02em;
  }
  .card { width: 100%; padding: 0 50px; }
  .divider { height: 1px; background: var(--border); margin: 1.25rem 0; }
  .status-row {
    display: flex;
    align-items: center;
    gap: 0.6rem;
    font-family: var(--mono);
    font-size: 13px;
  }
  #dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
  #dot.up { background: var(--accent); }
  #dot.down { background: var(--error); }
  .field { display: flex; flex-direction: column; gap: 0.5rem; margin-bottom: 1.25rem; }
  .field label {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text-dim);
  }
  .input-row { display: flex; gap: 0.5rem; flex-wrap: wrap; }
  .input-row input[type="text"] {
    flex: 1;
    min-width: 180px;
    background: var(--surface);
    border: 1px solid var(--border2);
    color: var(--text);
    font-family: var(--mono);
    font-size: 14px;
    padding: 0.55rem 0.75rem;
    outline: none;
    transition: border-color 0.15s;
  }
  .input-row input[type="text"]:focus { border-color: var(--accent); }
  button {
    font-family: var(--display);
    font-weight: 600;
    font-size: 13px;
    letter-spacing: 0.01em;
    border-radius: 8px;
    padding: 0.55rem 1rem;
    border: none;
    cursor: pointer;
    transition: opacity 0.15s, transform 0.1s, border-color 0.15s, color 0.15s;
  }
  button:hover { opacity: 0.88; }
  button:active { transform: scale(0.98); }
  button:disabled { opacity: 0.35; cursor: not-allowed; transform: none; }
  .btn-accent { background: var(--accent); color: #fff; }
  .btn-purple { background: var(--hal-purp); color: #fff; }
  .btn-outline { background: transparent; border: 1px solid var(--border2); color: var(--text); opacity: 1; }
  .btn-outline:hover { border-color: var(--accent); color: var(--accent); opacity: 1; }
  .hint { font-family: var(--mono); font-size: 12px; color: var(--text-dim); min-height: 1.2em; }
  .checkbox-label {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    cursor: pointer;
    font-family: var(--mono);
    font-size: 12.5px;
    color: var(--text);
  }
  .checkbox-label input[type="checkbox"] { width: 16px; height: 16px; accent-color: var(--accent); cursor: pointer; }
  .adv-label {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-bottom: 0.6rem;
  }
  #log {
    background: var(--surface);
    border: 1px solid var(--border2);
    max-height: 300px;
    overflow-y: auto;
    padding: 0.25rem 0.75rem;
  }
  .entry {
    padding: 0.4rem 0;
    font-family: var(--mono);
    font-size: 12.5px;
    border-bottom: 1px solid #eee;
  }
  .entry:last-child { border-bottom: none; }
  .kind-tts { color: var(--accent); }
  .kind-command { color: var(--hal-purp); }
  .kind-system { color: var(--text-dim); }
  .kind-cache { color: var(--info); }
  .ts { color: var(--text-dim); margin-right: 0.6em; }
  footer {
    margin-top: 2rem;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text-dim);
    letter-spacing: 0.05em;
    text-align: center;
  }
</style>
</head>
<body>
  <header>
    <h1>GNOME BOT</h1>
  </header>
  <div class="card">
    <div class="status-row"><span id="dot" class="down"></span><span id="status">checking...</span></div>
    <div class="divider"></div>

    <div class="field">
      <label>Audio output directory</label>
      <div class="input-row">
        <input id="dirInput" type="text" spellcheck="false">
        <button class="btn-outline" id="dirBrowse">Browse...</button>
        <button class="btn-accent" id="dirSave">Save</button>
        <button class="btn-purple" id="testTts">Test</button>
      </div>
      <div class="hint" id="dirMsg"></div>
    </div>

    <div class="field">
      <label>Twitch channel (for !gnomevoice and the special commands)</label>
      <div class="input-row">
        <input id="channelInput" type="text" spellcheck="false" placeholder="channel name, no #">
        <button class="btn-accent" id="channelSave">Save</button>
      </div>
      <div class="hint" id="channelMsg"></div>
    </div>

    <div class="field">
      <label class="checkbox-label"><input type="checkbox" id="chatToggle"> Also speak normal chat messages (in addition to the redeem)</label>
    </div>

    <div class="divider"></div>
    <div class="adv-label">Activity log</div>
    <div id="log"></div>
  </div>
  <footer>gnome bot &middot; local status page</footer>

<script>
let dirInitialized = false;
let channelInitialized = false;
let chatInitialized = false;

async function refresh() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    document.getElementById('dot').className = data.connected ? 'up' : 'down';
    document.getElementById('status').textContent = data.connected
      ? 'Connected to Half-Life: Alyx' : 'Waiting for Half-Life: Alyx...';

    const dirInput = document.getElementById('dirInput');
    if (!dirInitialized && document.activeElement !== dirInput) {
      dirInput.value = data.output_dir;
      dirInitialized = true;
    }

    const channelInput = document.getElementById('channelInput');
    if (!channelInitialized && document.activeElement !== channelInput) {
      channelInput.value = data.twitch_channel;
      channelInitialized = true;
    }

    const chatToggle = document.getElementById('chatToggle');
    if (!chatInitialized) {
      chatToggle.checked = data.chat_enabled;
      chatInitialized = true;
    }

    document.getElementById('log').innerHTML = data.log.map(function(e) {
      return '<div class="entry kind-' + e.kind + '">' +
             '<span class="ts">' + e.ts + '</span>' + escapeHtml(e.text) + '</div>';
    }).join('');
  } catch (e) { /* server not up yet, just try again next tick */ }
}
function escapeHtml(s) {
  return s.replace(/[&<>]/g, function(c) { return {'&':'&amp;','<':'&lt;','>':'&gt;'}[c]; });
}

document.getElementById('dirSave').addEventListener('click', async function() {
  const path = document.getElementById('dirInput').value;
  const msg = document.getElementById('dirMsg');
  msg.textContent = 'Saving...';
  try {
    const res = await fetch('/api/set_output_dir', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: path})
    });
    const data = await res.json();
    msg.textContent = data.ok ? 'Saved.' : ('Error: ' + data.error);
  } catch (e) {
    msg.textContent = 'Error: could not reach server';
  }
});

document.getElementById('dirBrowse').addEventListener('click', async function() {
  const msg = document.getElementById('dirMsg');
  const btn = this;
  btn.disabled = true;
  msg.textContent = 'Opening folder browser (check your taskbar if it doesn\u2019t pop to front)...';
  try {
    const res = await fetch('/api/browse_output_dir', { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      document.getElementById('dirInput').value = data.output_dir;
      msg.textContent = 'Saved.';
    } else if (data.cancelled) {
      msg.textContent = '';
    } else {
      msg.textContent = 'Error: ' + data.error;
    }
  } catch (e) {
    msg.textContent = 'Error: could not reach server';
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('channelSave').addEventListener('click', async function() {
  const channel = document.getElementById('channelInput').value;
  const msg = document.getElementById('channelMsg');
  msg.textContent = 'Saving...';
  try {
    const res = await fetch('/api/set_twitch_channel', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({channel: channel})
    });
    const data = await res.json();
    msg.textContent = data.ok ? 'Saved.' : ('Error: ' + data.error);
  } catch (e) {
    msg.textContent = 'Error: could not reach server';
  }
});

document.getElementById('testTts').addEventListener('click', async function() {
  const msg = document.getElementById('dirMsg');
  const btn = this;
  btn.disabled = true;
  msg.textContent = 'Playing test clip...';
  try {
    const res = await fetch('/api/test_tts', { method: 'POST' });
    const data = await res.json();
    msg.textContent = data.ok ? 'Queued -- check the log below.' : ('Error: ' + data.error);
  } catch (e) {
    msg.textContent = 'Error: could not reach server';
  } finally {
    btn.disabled = false;
  }
});

document.getElementById('chatToggle').addEventListener('change', async function() {
  const enabled = this.checked;
  try {
    await fetch('/api/set_chat_enabled', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: enabled})
    });
  } catch (e) { /* log/status on the page will reflect reality next refresh either way */ }
});

refresh();
setInterval(refresh, 1000);
</script>
</body>
</html>
"""


class _UIRequestHandler(BaseHTTPRequestHandler):
    ui_state: UIState = None  # set per-server via a subclass in run_ui_server()

    def do_GET(self):
        if self.path == "/":
            body = _PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            body = json.dumps(self.ui_state.snapshot()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        endpoints = {
            "/api/set_output_dir": self._handle_set_output_dir,
            "/api/browse_output_dir": self._handle_browse_output_dir,
            "/api/set_twitch_channel": self._handle_set_twitch_channel,
            "/api/set_chat_enabled": self._handle_set_chat_enabled,
            "/api/test_tts": self._handle_test_tts,
        }
        handler = endpoints.get(self.path)
        if handler is None:
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError) as e:
            self._send_json(400, {"ok": False, "error": f"Bad request body: {e}"})
            return
        handler(payload)

    def _handle_set_output_dir(self, payload: dict):
        try:
            self.ui_state.set_output_dir(str(payload.get("path", "")))
        except (ValueError, OSError) as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True, "output_dir": self.ui_state.output_dir})

    def _handle_browse_output_dir(self, payload: dict):
        try:
            chosen = _browse_for_directory(self.ui_state.output_dir)
        except RuntimeError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        if not chosen:
            self._send_json(200, {"ok": False, "cancelled": True})
            return
        try:
            self.ui_state.set_output_dir(chosen)
        except (ValueError, OSError) as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True, "output_dir": self.ui_state.output_dir})

    def _handle_set_twitch_channel(self, payload: dict):
        try:
            self.ui_state.set_twitch_channel(str(payload.get("channel", "")))
        except ValueError as e:
            self._send_json(400, {"ok": False, "error": str(e)})
            return
        self._send_json(200, {"ok": True, "twitch_channel": self.ui_state.twitch_channel})

    def _handle_set_chat_enabled(self, payload: dict):
        self.ui_state.set_chat_enabled(bool(payload.get("enabled", False)))
        self._send_json(200, {"ok": True, "chat_enabled": self.ui_state.chat_enabled})

    def _handle_test_tts(self, payload: dict):
        ok = self.ui_state.request_test()
        if not ok:
            self._send_json(400, {"ok": False,
                                   "error": "Not ready yet -- the bot is still starting up"})
            return
        self._send_json(200, {"ok": True})

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # silence per-request stdout logging -- the page itself is the log


def run_ui_server(ui_state: UIState, host: str = DEFAULT_UI_HOST, port: int = DEFAULT_UI_PORT):
    """
    Blocks forever serving the status page; run this in its own thread.
    ThreadingHTTPServer (rather than plain HTTPServer) matters here
    specifically because of the native folder-browse dialog: it blocks
    its own request for as long as the dialog is open, and without
    threading that would also freeze the /api/status polling for anyone
    else looking at the page in the meantime.
    """
    handler_cls = type("_BoundUIRequestHandler", (_UIRequestHandler,), {"ui_state": ui_state})
    server = ThreadingHTTPServer((host, port), handler_cls)
    print(f"[gnome_ui] Status page: http://{host}:{port}/")
    server.serve_forever()
