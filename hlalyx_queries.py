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
        self._client = _CapturingClient(host, port, verbose=verbose)
        self._timeout = timeout
        self._listener: Optional[threading.Thread] = None
        self.debug_timing = debug_timing
        self._latencies: Dict[str, "deque[float]"] = {
            "getpos": deque(maxlen=100),
            "print_ents": deque(maxlen=100),
        }

    def connect(self):
        self._client.connect()
        self._listener = threading.Thread(target=self._client._listen_loop, daemon=True)
        self._listener.start()
        time.sleep(0.2)  # give the initial CHAN channel table a moment to arrive

    def close(self):
        self._client.close()

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
        sent_at = time.time()
        for _, cmd, _, _ in commands:
            self._client.send_command(cmd)

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