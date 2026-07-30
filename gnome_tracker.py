"""
gnome_tracker.py

Periodically polls Gnome Chompski's world position and the player's
position/view-angles via HLAlyxQueries, and reports the gnome's location
*relative to the player* (distance, and bearing/elevation off of where
Alyx is currently looking).

All the actual spatial math now lives in gnome_spatial.py (shared with the
Twitch bot), which also fixes the left/right bearing sign that was
inverted in the previous version.
"""

import time

from hlalyx_queries import HLAlyxQueries
from gnome_spatial import relative_position, describe, closest_to_player

POLL_INTERVAL_SECONDS = 1.0


def poll_loop(queries: HLAlyxQueries, interval: float = POLL_INTERVAL_SECONDS):
    while True:
        if not queries.connected:
            # _print_status_change already reported this; just wait for
            # the background auto-reconnect thread to pick the game up.
            time.sleep(interval)
            continue

        player_pose = queries.get_player_pose()
        gnome_origins = queries.get_gnome_origins()

        if player_pose is None:
            print("[gnome_tracker] Couldn't read player pose right now.")
        elif not gnome_origins:
            print("[gnome_tracker] Couldn't find any gnome.vmdl entities right now.")
        else:
            gnome_origin = (closest_to_player(gnome_origins, player_pose)
                             if len(gnome_origins) > 1 else gnome_origins[0])
            distance, bearing, elevation = relative_position(gnome_origin, player_pose)
            print(f"[gnome_tracker] {describe(distance, bearing, elevation)}")

        time.sleep(interval)


def _print_status_change(connected: bool):
    print("[gnome_tracker] Connected to Half-Life: Alyx." if connected
          else "[gnome_tracker] Half-Life: Alyx not found -- waiting (checking every second)...")


if __name__ == "__main__":
    queries = HLAlyxQueries(host="127.0.0.1", port=29000)
    # start() doesn't require the game to already be running: it retries
    # the connection every second in the background and keeps retrying
    # again automatically if the game later closes.
    queries.start(retry_interval=1.0, on_status_change=_print_status_change)
    try:
        poll_loop(queries)
    except KeyboardInterrupt:
        print("Stopped.")
    finally:
        queries.close()