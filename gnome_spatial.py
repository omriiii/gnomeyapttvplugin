"""
gnome_spatial.py

Shared spatial math used by both the console gnome_tracker.py script and
the Twitch bot's positional-audio playback, plus a background GnomeTracker
that keeps the last two known gnome positions so callers can interpolate
motion between them instead of snapping every poll.

BUGFIX vs. the previous version: bearing was mirrored (left/right flipped).
The old world_to_local() rotation produced a "right" component that was the
exact negative of Source's own right-hand vector (verified against the
engine's AngleVectors() formula: forward=(cp*cy,cp*sy,-sp),
right=(sy,-cy,0), up=(sp*cy,sp*sy,cp), roll ignored). This version projects
the world-space delta directly onto those basis vectors instead, which
also happens to be simpler than the old inverse-rotation approach.
"""

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional, Tuple, List

from hlalyx_queries import HLAlyxQueries, Vec3, Pose


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_vec3(a: Vec3, b: Vec3, t: float) -> Vec3:
    return Vec3(lerp(a.x, b.x, t), lerp(a.y, b.y, t), lerp(a.z, b.z, t))


def _basis_vectors(yaw_deg: float, pitch_deg: float):
    """(forward, right, up) unit vectors, matching Source's AngleVectors()
    (roll ignored -- it barely affects yaw/pitch-driven bearing/elevation)."""
    yaw = math.radians(yaw_deg)
    pitch = math.radians(pitch_deg)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)

    forward = Vec3(cp * cy, cp * sy, -sp)
    right = Vec3(sy, -cy, 0.0)
    up = Vec3(sp * cy, sp * sy, cp)
    return forward, right, up


def _dot(a: Vec3, b: Vec3) -> float:
    return a.x * b.x + a.y * b.y + a.z * b.z


def relative_position(target: Vec3, player: Pose) -> Tuple[float, float, float]:
    """
    Returns (distance, bearing_deg, elevation_deg) of `target` relative to
    `player`'s position + view direction.
      bearing:   0 = straight ahead, + = to the right, - = to the left
      elevation: 0 = level,          + = above,        - = below
    """
    delta = Vec3(target.x - player.origin.x,
                 target.y - player.origin.y,
                 target.z - player.origin.z)

    forward, right, up = _basis_vectors(player.yaw, player.pitch)
    f, r, u = _dot(delta, forward), _dot(delta, right), _dot(delta, up)

    distance = math.sqrt(f * f + r * r + u * u)
    bearing = math.degrees(math.atan2(r, f))
    elevation = math.degrees(math.atan2(u, math.hypot(f, r)))
    return distance, bearing, elevation


def describe(distance: float, bearing: float, elevation: float) -> str:
    lr = "right" if bearing > 2 else "left" if bearing < -2 else "straight ahead"
    ud = "above" if elevation > 2 else "below" if elevation < -2 else "level"
    return (f"{distance:6.1f} units away, "
            f"{abs(bearing):5.1f}\u00b0 {lr}, "
            f"{abs(elevation):5.1f}\u00b0 {ud}")


def closest_to_player(origins, player: Pose) -> Vec3:
    return min(
        origins,
        key=lambda o: (o.x - player.origin.x) ** 2
        + (o.y - player.origin.y) ** 2
        + (o.z - player.origin.z) ** 2,
    )


@dataclass
class _Sample:
    t: float
    pos: Vec3


class GnomeTracker:
    """
    Polls HLAlyxQueries on a single background thread that independently
    schedules two cadences:
      - player pose, polled fast (player_poll_interval, default 50ms) since
        it directly drives audio panning and needs to feel instant on a
        head-turn
      - gnome position, polled slower (gnome_poll_interval, default 250ms)
        since a physical prop doesn't move nearly as fast as your head does

    Whenever both are due on the same tick (which happens once every
    gnome_poll_interval, since it's slower), it uses HLAlyxQueries.get_state()
    to fetch both in one pipelined round trip instead of two sequential
    ones. The rest of the time (the much more frequent case) it only fetches
    player pose, so head-turn responsiveness isn't held hostage by the
    slower gnome query at all.

    get_interpolated_gnome_pos(now) lerps between the gnome's last two
    samples using wall-clock time, so a several-second TTS clip can
    smoothly slide the gnome's apparent position across its poll interval
    instead of jumping every time a new sample arrives.
    """

    def __init__(self, queries: HLAlyxQueries,
                 player_poll_interval: float = 0.05,
                 gnome_poll_interval: float = 0.25):
        self._queries = queries
        self._player_poll_interval = player_poll_interval
        self._gnome_poll_interval = gnome_poll_interval
        self._samples: deque = deque(maxlen=2)
        self._player_pose: Optional[Pose] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _store_player_pose(self, pose: Optional[Pose]):
        if pose is not None:
            with self._lock:
                self._player_pose = pose

    def _store_gnome_origins(self, origins: List[Vec3]):
        if not origins:
            return
        with self._lock:
            player_pose = self._player_pose
        pos = (closest_to_player(origins, player_pose)
               if (len(origins) > 1 and player_pose)
               else origins[0])
        with self._lock:
            self._samples.append(_Sample(t=time.time(), pos=pos))

    def _run(self):
        next_player_due = time.time()
        next_gnome_due = time.time()

        while not self._stop.is_set():
            now = time.time()
            need_player = now >= next_player_due
            need_gnome = now >= next_gnome_due

            if need_player and need_gnome:
                pose, origins = self._queries.get_state()
                self._store_player_pose(pose)
                self._store_gnome_origins(origins)
                # Increment from the scheduled time, not "now", so the two
                # cadences stay aligned instead of drifting apart tick by
                # tick -- that alignment is what makes them keep landing on
                # the same tick and actually get batched together.
                next_player_due += self._player_poll_interval
                next_gnome_due += self._gnome_poll_interval
            elif need_player:
                self._store_player_pose(self._queries.get_player_pose())
                next_player_due += self._player_poll_interval
            elif need_gnome:
                self._store_gnome_origins(self._queries.get_gnome_origins())
                next_gnome_due += self._gnome_poll_interval
            else:
                time.sleep(max(0.0, min(next_player_due, next_gnome_due) - now))

            # Safety valve: if something stalled hard (e.g. a slow query)
            # and a due time fell far behind, snap it back to now instead
            # of firing a burst of rapid catch-up calls.
            now2 = time.time()
            if now2 - next_player_due > self._player_poll_interval * 4:
                next_player_due = now2
            if now2 - next_gnome_due > self._gnome_poll_interval * 4:
                next_gnome_due = now2

    def get_interpolated_gnome_pos(self, now: Optional[float] = None) -> Optional[Vec3]:
        """
        Interpolates between the second-to-last and last known gnome
        samples (the "start" and "target" of the lerp). If `now` is past
        the latest sample it extrapolates briefly along the same motion,
        clamped so a stalled poll doesn't send the gnome flying off.
        """
        now = now if now is not None else time.time()
        with self._lock:
            samples = list(self._samples)

        if not samples:
            return None
        if len(samples) == 1:
            return samples[0].pos

        prev, last = samples[0], samples[1]
        span = last.t - prev.t
        if span <= 0:
            return last.pos

        t = (now - prev.t) / span
        t = max(0.0, min(t, 2.0))  # allow slight extrapolation, then clamp
        return lerp_vec3(prev.pos, last.pos, t)

    def get_player_pose(self) -> Optional[Pose]:
        with self._lock:
            return self._player_pose

    def get_relative_now(self, now: Optional[float] = None):
        """Convenience: interpolated gnome position -> relative_position()."""
        player_pose = self.get_player_pose()
        gnome_pos = self.get_interpolated_gnome_pos(now)
        if player_pose is None or gnome_pos is None:
            return None
        return relative_position(gnome_pos, player_pose)