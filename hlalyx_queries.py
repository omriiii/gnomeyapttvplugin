"""
hlalyx_queries.py

High-level query layer built directly on top of hlalyx_console.VConsoleClient.
Does not reimplement any socket/protocol logic -- it subclasses VConsoleClient
just enough to (a) capture incoming console lines into a buffer instead of
only printing them, and (b) disable Nagle's algorithm on the socket, which
turned out to be the biggest single latency culprit (see below).

Exposes:
    - get_player_pose()    -> Pose        (via `getpos`)
    - get_gnome_origins()  -> list[Vec3]   (via `print_ents "gnome.vmdl" getorigin`)
    - get_state()          -> (Pose, list[Vec3])  -- BOTH, pipelined in one round trip

PERFORMANCE NOTES:
  - TCP_NODELAY: by default Python sockets leave Nagle's algorithm on,
    which buffers small outgoing packets to coalesce them. Combined with
    delayed ACKs on the other end, this is a well-known cause of
    ~100-800ms stalls on exactly this kind of small-message, frequent,
    bidirectional traffic. This is almost certainly why a single `getpos`
    (one line of output) was taking 800ms+ -- there's no reason for that
    much latency otherwise. Disabling it (setsockopt TCP_NODELAY) should
    be the single biggest win here.
  - Batching/pipelining: get_state() sends BOTH `getpos` and print_ents in
    immediate succession (not waiting for the first command's response
    before sending the second), then waits for both results together.
    Whatever fixed per-command round-trip cost remains gets paid once
    instead of twice.
  - The previous version always waited a fixed ~150ms "settle" period
    after a command's output stopped growing before returning, even for
    `getpos` which only ever produces one line. get_player_pose() (and
    get_state()'s getpos half) returns as soon as its one match arrives;
    only the print_ents side still uses settle-time collection, since it
    genuinely needs to wait for a multi-row table to finish arriving.
  - 22 matches for "gnome.vmdl" is a lot -- if most of those are unrelated
    decorative props sharing that model rather than the one prop you're
    tracking, a tighter print_ents pattern (matching a unique entity name
    instead of the model) would cut both the wait time and the amount of
    data being parsed. Worth checking with print_ents "gnome.vmdl" getname
    to see what's actually matching.
  - verbose=False by default: console lines are still captured into the
    buffer for parsing, but no longer printed to stdout.
  - debug_timing=True records round-trip latency for every query (see
    get_latency_stats()).
"""

import re
import socket
import time
import threading
from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Dict, Tuple

from hlalyx_console import VConsoleClient


@dataclass
class Vec3:
    x: float
    y: float
    z: float


@dataclass
class Pose:
    """Position + view angles (pitch, yaw, roll in degrees, Source convention)."""
    origin: Vec3
    pitch: float
    yaw: float
    roll: float


class _CapturingClient(VConsoleClient):
    """
    Identical to VConsoleClient in every way that matters for the socket
    protocol -- it just stashes each decoded console line (with an arrival
    timestamp) into a buffer instead of printing it, and disables Nagle's
    algorithm on the socket once connected.
    """

    def __init__(self, *args, verbose: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self._lines = deque(maxlen=2000)
        self._lines_lock = threading.Lock()
        self.verbose = verbose

    def connect(self):
        super().connect()
        # This is the big one: without this, small frequent send/recv
        # traffic like ours can stall for hundreds of ms due to Nagle's
        # algorithm interacting with delayed ACKs on the other end.
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def _handle_prnt(self, body: bytes):
        if len(body) >= 28:
            msg = body[28:].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            with self._lines_lock:
                self._lines.append((time.time(), msg))
            if self.verbose:
                super()._handle_prnt(body)

    def lines_since(self, since_ts: float) -> List[str]:
        with self._lines_lock:
            return [msg for ts, msg in self._lines if ts >= since_ts]


class HLAlyxQueries:
    """
    Thin query wrapper around the VConsole2 socket connection from
    hlalyx_console.py.
    """

    _GETPOS_RE = re.compile(
        r"setpos\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)"
        r";\s*setang\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)\s+(-?\d+\.?\d*)"
    )
    _ORIGIN_ROW_RE = re.compile(
        r"\[\d+\]\s+0x[0-9a-fA-F]+\s*\|\s*\[\s*"
        r"(-?\d+\.?\d*),\s*(-?\d+\.?\d*),\s*(-?\d+\.?\d*)\s*\]"
    )

    def __init__(self, host="127.0.0.1", port=29000, timeout=1.5,
                 verbose: bool = False, debug_timing: bool = False):
        self._host = host
        self._port = port
        self._verbose = verbose
        self._client = _CapturingClient(host, port, verbose=verbose)
        self._timeout = timeout
        self._listener: Optional[threading.Thread] = None
        self.debug_timing = debug_timing
        self._latencies: Dict[str, "deque[float]"] = {
            "getpos": deque(maxlen=100),
            "print_ents": deque(maxlen=100),
        }

        # Auto-reconnect state (see start()/connected below). `connected`
        # is the thing callers/UIs should actually look at -- it reflects
        # whether the socket is up right now, independent of whether
        # auto-reconnect is running.
        self.connected = False
        self._auto_stop = threading.Event()
        self._auto_thread: Optional[threading.Thread] = None
        self._on_status_change = None

    def connect(self):
        """One-shot, blocking connect (raises OSError if HL:A isn't up).
        Prefer start() below if the game might not be running yet."""
        self._client.connect()
        self._listener = threading.Thread(target=self._client._listen_loop, daemon=True)
        self._listener.start()
        time.sleep(0.2)  # give the initial CHAN channel table a moment to arrive
        self.connected = True

    def start(self, retry_interval: float = 1.0, on_status_change=None):
        """
        Non-blocking alternative to connect(): starts a background thread
        that keeps trying to connect every `retry_interval` seconds until
        it succeeds, and keeps watching afterwards so that if the game
        closes (or the listener thread dies for any other reason) it goes
        back to retrying automatically instead of leaving the caller with
        a silently-dead connection.

        `connected` reflects live status at all times; pass
        `on_status_change(bool)` if you want a callback (e.g. to drive a
        status dot in a UI) instead of polling `connected` yourself.
        """
        self._on_status_change = on_status_change
        self._auto_thread = threading.Thread(target=self._auto_connect_loop,
                                              args=(retry_interval,), daemon=True)
        self._auto_thread.start()

    def _set_connected(self, value: bool):
        if value == self.connected:
            return
        self.connected = value
        if self._on_status_change:
            try:
                self._on_status_change(value)
            except Exception:
                pass  # a broken UI callback shouldn't take down the retry loop

    def _auto_connect_loop(self, retry_interval: float):
        while not self._auto_stop.is_set():
            if not self.connected:
                # Fresh client each attempt -- a socket that failed to
                # connect (or got closed on disconnect) can't be reused.
                self._client = _CapturingClient(self._host, self._port, verbose=self._verbose)
                try:
                    self._client.connect()
                except OSError:
                    self._auto_stop.wait(retry_interval)
                    continue
                self._listener = threading.Thread(target=self._client._listen_loop, daemon=True)
                self._listener.start()
                time.sleep(0.2)  # let the initial CHAN table arrive
                self._set_connected(True)
            else:
                # Detect the game having gone away: the listen loop exits
                # on socket close/error (see VConsoleClient._listen_loop).
                if self._listener is not None and not self._listener.is_alive():
                    self._set_connected(False)
                    continue
                self._auto_stop.wait(retry_interval)

    def close(self):
        self._auto_stop.set()
        self._client.close()
        self._set_connected(False)

    def send_command(self, command: str) -> bool:
        """
        Sends a raw console command if currently connected to HL:A.
        Returns True/False instead of raising, so callers (like
        chat-triggered commands) can just check the result rather than
        wrapping every call in a try/except.
        """
        if not self.connected:
            return False
        try:
            self._client.send_command(command)
            return True
        except (ConnectionError, OSError):
            return False

    # ------------------------------------------------------------------ #
    # Timing / diagnostics
    # ------------------------------------------------------------------ #
    def get_latency_stats(self, label: str) -> Optional[dict]:
        samples = list(self._latencies.get(label, ()))
        if not samples:
            return None
        return {
            "last": samples[-1],
            "min": min(samples),
            "avg": sum(samples) / len(samples),
            "max": max(samples),
            "n": len(samples),
        }

    def _record_latency(self, label: str, elapsed: float):
        self._latencies[label].append(elapsed)
        if self.debug_timing:
            print(f"[hlalyx_queries] {label} took {elapsed * 1000:.1f}ms")

    # ------------------------------------------------------------------ #
    # Core dispatcher: send one or more commands back-to-back (pipelined),
    # then collect matches for each until each is individually "done".
    # ------------------------------------------------------------------ #
    def _query_multi(self, commands: List[Tuple[str, str, "re.Pattern", bool]],
                      settle_time: float = 0.15) -> Dict[str, List["re.Match"]]:
        """
        commands: list of (label, command_str, pattern, wait_for_settle)

        Sends every command immediately, one after another, WITHOUT waiting
        for a response in between (that's the pipelining/batching) -- then
        polls for all of their outputs concurrently. Each command finishes
        independently: wait_for_settle=False commands are done as soon as
        their first match appears; wait_for_settle=True commands are done
        once their match count stops growing for `settle_time` seconds.
        """
        if not self.connected:
            # HL:A isn't up (yet, or anymore) -- nothing to send to. Return
            # empty results instead of throwing, so pollers can just treat
            # this like "no data right now" and try again next tick.
            return {label: [] for label, _, _, _ in commands}

        sent_at = time.time()
        try:
            for _, cmd, _, _ in commands:
                self._client.send_command(cmd)
        except (ConnectionError, OSError):
            # Connection dropped between the check above and the send --
            # the auto-reconnect loop (if running) will notice and retry.
            return {label: [] for label, _, _, _ in commands}

        deadline = sent_at + self._timeout
        state = {
            label: {"matches": [], "last_growth": sent_at, "done": False, "done_time": None}
            for label, _, _, _ in commands
        }

        while time.time() < deadline and not all(s["done"] for s in state.values()):
            lines = self._client.lines_since(sent_at)
            now = time.time()
            for label, _, pattern, wait_for_settle in commands:
                s = state[label]
                if s["done"]:
                    continue
                found = [m for line in lines for m in pattern.finditer(line)]
                if len(found) > len(s["matches"]):
                    s["matches"] = found
                    s["last_growth"] = now
                    if not wait_for_settle:
                        s["done"] = True
                        s["done_time"] = now
                elif s["matches"] and wait_for_settle and (now - s["last_growth"]) > settle_time:
                    s["done"] = True
                    s["done_time"] = now
            time.sleep(0.005)

        finish = time.time()
        for label, _, _, _ in commands:
            s = state[label]
            self._record_latency(label, (s["done_time"] or finish) - sent_at)

        return {label: state[label]["matches"] for label, _, _, _ in commands}

    def _query(self, command: str, pattern: "re.Pattern", label: str,
               wait_for_settle: bool = True) -> List["re.Match"]:
        return self._query_multi([(label, command, pattern, wait_for_settle)])[label]

    # ------------------------------------------------------------------ #
    # Built-in queries
    # ------------------------------------------------------------------ #
    def get_player_pose(self) -> Optional[Pose]:
        """Runs `getpos` alone and returns the player's position + view angles."""
        matches = self._query("getpos", self._GETPOS_RE, label="getpos", wait_for_settle=False)
        return self._parse_pose(matches)

    def get_gnome_origins(self) -> List[Vec3]:
        """Runs print_ents "gnome.vmdl" getorigin alone and returns every matched origin."""
        matches = self._query('print_ents "gnome.vmdl" getorigin', self._ORIGIN_ROW_RE,
                               label="print_ents", wait_for_settle=True)
        return self._parse_origins(matches)

    def get_state(self) -> Tuple[Optional[Pose], List[Vec3]]:
        """
        Batched/pipelined version of get_player_pose() + get_gnome_origins():
        sends both commands immediately back-to-back instead of sequentially
        waiting on one before sending the other. Use this when you need both
        values together; use the individual methods when you only need one
        (e.g. a fast player-pose-only polling loop).
        """
        results = self._query_multi([
            ("getpos", "getpos", self._GETPOS_RE, False),
            ("print_ents", 'print_ents "gnome.vmdl" getorigin', self._ORIGIN_ROW_RE, True),
        ])
        return self._parse_pose(results["getpos"]), self._parse_origins(results["print_ents"])

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #
    def _parse_pose(self, matches: List["re.Match"]) -> Optional[Pose]:
        if not matches:
            return None
        x, y, z, pitch, yaw, roll = (float(v) for v in matches[-1].groups())
        return Pose(origin=Vec3(x, y, z), pitch=pitch, yaw=yaw, roll=roll)

    def _parse_origins(self, matches: List["re.Match"]) -> List[Vec3]:
        return [Vec3(float(x), float(y), float(z)) for x, y, z in (m.groups() for m in matches)]