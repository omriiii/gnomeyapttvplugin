"""
hlalyx_latency_check.py

Standalone diagnostic: connects to the HL:A VConsole2 socket and repeatedly
calls get_player_pose() / get_gnome_origins(), timing each call directly
with time.perf_counter() (independent of HLAlyxQueries' own internal
debug_timing, as a sanity cross-check), then prints min/avg/max/last
latency for each.

Run this with the game up (launched with -tools) to see exactly how long
each query is taking on your machine. If get_player_pose() latency is high,
that's your head-turn lag right there -- audio panning can't respond any
faster than that number.

Usage:
    python hlalyx_latency_check.py --iterations 50
"""

import argparse
import time

from hlalyx_queries import HLAlyxQueries


def summarize(label: str, samples: list):
    if not samples:
        print(f"{label}: no successful samples")
        return
    print(f"{label}: n={len(samples)}  "
          f"min={min(samples)*1000:6.1f}ms  "
          f"avg={sum(samples)/len(samples)*1000:6.1f}ms  "
          f"max={max(samples)*1000:6.1f}ms")


def main():
    parser = argparse.ArgumentParser(description="Measure HL:A console query round-trip latency")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=29000)
    parser.add_argument("--iterations", type=int, default=30)
    args = parser.parse_args()

    queries = HLAlyxQueries(host=args.host, port=args.port, verbose=False, debug_timing=False)
    queries.connect()

    getpos_times = []
    gnome_times = []
    batched_times = []

    print(f"Running {args.iterations} iterations of getpos + print_ents (sequential)...\n")
    for i in range(args.iterations):
        t0 = time.perf_counter()
        pose = queries.get_player_pose()
        t1 = time.perf_counter()
        if pose is not None:
            getpos_times.append(t1 - t0)
        print(f"[{i+1}/{args.iterations}] getpos: {(t1 - t0)*1000:6.1f}ms  "
              f"{'(no response)' if pose is None else ''}")

        t0 = time.perf_counter()
        origins = queries.get_gnome_origins()
        t1 = time.perf_counter()
        if origins:
            gnome_times.append(t1 - t0)
        print(f"[{i+1}/{args.iterations}] print_ents: {(t1 - t0)*1000:6.1f}ms  "
              f"({len(origins)} match(es))")

    print(f"\nRunning {args.iterations} iterations of get_state() (batched/pipelined)...\n")
    for i in range(args.iterations):
        t0 = time.perf_counter()
        pose, origins = queries.get_state()
        t1 = time.perf_counter()
        batched_times.append(t1 - t0)
        print(f"[{i+1}/{args.iterations}] get_state: {(t1 - t0)*1000:6.1f}ms  "
              f"(pose={'ok' if pose else 'missing'}, {len(origins)} gnome match(es))")

    print()
    summarize("getpos (sequential)", getpos_times)
    summarize("print_ents (sequential)", gnome_times)
    summarize("get_state (batched)", batched_times)
    if getpos_times and gnome_times and batched_times:
        seq_total = sum(getpos_times) / len(getpos_times) + sum(gnome_times) / len(gnome_times)
        batched_avg = sum(batched_times) / len(batched_times)
        print(f"\nsequential avg total (getpos+print_ents): {seq_total*1000:.1f}ms")
        print(f"batched avg (get_state):                  {batched_avg*1000:.1f}ms")

    # Also compare against HLAlyxQueries' own built-in tracking, as a
    # sanity check that both measurements agree.
    print()
    for label in ("getpos", "print_ents"):
        stats = queries.get_latency_stats(label)
        if stats:
            print(f"(internal) {label}: last={stats['last']*1000:.1f}ms  "
                  f"avg={stats['avg']*1000:.1f}ms  n={stats['n']}")

    queries.close()


if __name__ == "__main__":
    main()