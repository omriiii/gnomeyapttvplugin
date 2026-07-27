#!/usr/bin/env python3
"""
hlalyx_console.py

A simple VConsole2-style client for Half-Life: Alyx (and other Source 2 games).

Half-Life: Alyx exposes its developer console over a plain TCP socket on
127.0.0.1:29000 (the same mechanism vconsole2.exe / VConsole2 uses). This
script connects to that socket, decodes the incoming binary protocol so you
can read console output (log lines, tagged by channel), and lets you type
console commands that get sent back to the game.

Requirements on the game side:
  - Launch HL:Alyx with the "-tools" launch option (Steam -> right click
    Half-Life: Alyx -> Properties -> Launch Options -> add -tools), which
    enables the vconsole listener.
  - If port 29000 is already taken (SteamVR's vrserver.exe sometimes grabs
    it), add "-vconport <port>" to the launch options and pass --port to
    this script to match.

Protocol notes (reverse engineered / ported from the open-source
VConsoleLib / VConsole2Lib.python projects):

  Every message, in both directions, starts with a 12-byte header:

      4s   msg_type      ASCII tag, e.g. b"PRNT", b"CHAN", b"CMND"
      i    version       big-endian int32 (protocol version)
      h    length        big-endian int16, TOTAL size of the packet
                          (i.e. counted from the start of msg_type,
                          including this header)
      h    handle        big-endian int16 (session handle, 0 is fine
                          for commands we send)

  followed by (length - 12) bytes of body, whose layout depends on
  msg_type. The two message types we care about:

    PRNT (game -> us): a console output line.
        i        channel_id
        24 bytes unknown/reserved
        remaining bytes: the message text, NUL-terminated

    CHAN (game -> us): the table mapping channel IDs to names, sent
        once after connecting. Lets us print e.g. "[Console]" instead
        of a raw channel number.
        h        channel count
        then, repeated `count` times (58 bytes each):
            i i i i i   id, unknown1, unknown2, verbosity_default,
                        verbosity_current
            4 bytes     RGBA colour override
            34 bytes    channel name, NUL-terminated

  To send a console command, we build a CMND packet ourselves:
        msg_type = b"CMND"
        version  = whatever version number the game itself has been
                   sending us in its own packets (learned automatically
                   at runtime -- see protocol_version below). Getting
                   this wrong is the most common reason commands get
                   silently ignored while console output still works
                   fine in the other direction.
        body     = command text + NUL terminator

  Other message types (AINF, ADON, CVAR, CFGV, ...) are read and
  discarded here since we don't need them just to watch logs and send
  commands, but the length-prefixed framing means skipping them is safe.
"""

import argparse
import socket
import struct
import sys
import threading

HEADER_FMT = ">4sihh"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 12 bytes

CMND_VERSION = 0xD2  # fallback only, used until we learn the real version from the game (see below)


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from a socket (recv() can return short reads)."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Socket closed while reading")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


class VConsoleClient:
    def __init__(self, host="127.0.0.1", port=29000, debug=False):
        self.host = host
        self.port = port
        self.sock = None
        self.channels = {}          # channel_id -> channel name
        self._print_lock = threading.Lock()
        self._stop = threading.Event()
        self.debug = debug
        # The game tells us its own protocol version in every packet it
        # sends us. Rather than guessing a hardcoded value, we learn it
        # from the first packet we receive and echo it back in our own
        # CMND packets. If the version we send doesn't match what the
        # game expects, it silently drops the command -- no error, no
        # disconnect -- which is the most common reason "commands don't
        # work" while console output still comes through fine.
        self.protocol_version = CMND_VERSION

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))

    def close(self):
        self._stop.set()
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass

    def send_command(self, command: str):
        """Send a console command, exactly like typing it in-game."""
        if self.sock is None:
            raise RuntimeError("Not connected")
        payload = command.encode("utf-8", errors="replace") + b"\x00"
        total_length = HEADER_SIZE + len(payload)
        packet = struct.pack(HEADER_FMT, b"CMND", self.protocol_version, total_length, 0) + payload
        if self.debug:
            self._safe_print(f"[debug] sending: {packet!r}")
        self.sock.sendall(packet)

    def _safe_print(self, text: str):
        with self._print_lock:
            print(text, flush=True)

    def _handle_prnt(self, body: bytes):
        if len(body) < 28:
            return
        channel_id = struct.unpack_from(">i", body, 0)[0]
        msg = body[28:].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        channel_name = self.channels.get(channel_id, f"#{channel_id}")
        self._safe_print(f"[{channel_name}] {msg}")

    def _handle_chan(self, body: bytes):
        if len(body) < 2:
            return
        count = struct.unpack_from(">h", body, 0)[0]
        offset = 2
        entry_size = 20 + 4 + 34  # 5 int32s + RGBA + name
        for _ in range(count):
            if offset + entry_size > len(body):
                break
            channel_id = struct.unpack_from(">i", body, offset)[0]
            name_bytes = body[offset + 24: offset + entry_size]
            name = name_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            self.channels[channel_id] = name
            offset += entry_size

    def _listen_loop(self):
        try:
            while not self._stop.is_set():
                header_bytes = recv_exact(self.sock, HEADER_SIZE)
                msg_type, version, length, handle = struct.unpack(HEADER_FMT, header_bytes)
                body_size = length - HEADER_SIZE
                body = recv_exact(self.sock, body_size) if body_size > 0 else b""

                self.protocol_version = version
                if self.debug:
                    self._safe_print(f"[debug] recv: type={msg_type!r} version={version:#x} "
                                      f"length={length} handle={handle}")

                if msg_type == b"PRNT":
                    self._handle_prnt(body)
                elif msg_type == b"CHAN":
                    self._handle_chan(body)
                # Other message types (AINF, ADON, CVAR, CFGV, ...) are just
                # consumed above and otherwise ignored -- add elif branches
                # here if you want to decode more of them later.

        except (ConnectionError, OSError):
            if not self._stop.is_set():
                self._safe_print("\n[disconnected from Half-Life: Alyx]")


def main():
    parser = argparse.ArgumentParser(description="VConsole2-style client for Half-Life: Alyx")
    parser.add_argument("--host", default="127.0.0.1", help="Game console host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=29000, help="Game console port (default: 29000)")
    parser.add_argument("--debug", action="store_true",
                         help="Print raw packet headers for everything sent and received")
    args = parser.parse_args()

    client = VConsoleClient(args.host, args.port, debug=args.debug)

    print(f"Connecting to {args.host}:{args.port} ...")
    try:
        client.connect()
    except OSError as e:
        print(f"Could not connect: {e}")
        print("Make sure Half-Life: Alyx is running with -tools in its launch options,")
        print("and that nothing else (e.g. SteamVR's vrserver.exe) is holding port 29000.")
        sys.exit(1)

    print("Connected. Console output will print below.")
    print("Type a command and press Enter to send it. Type 'quit' or 'exit' to disconnect.\n")

    listener = threading.Thread(target=client._listen_loop, daemon=True)
    listener.start()

    try:
        while True:
            try:
                cmd = input()
            except EOFError:
                break
            if cmd.strip().lower() in ("quit", "exit"):
                break
            if cmd.strip() == "":
                continue
            try:
                client.send_command(cmd)
            except (ConnectionError, OSError) as e:
                print(f"Failed to send command: {e}")
                break
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
        print("Disconnected.")


if __name__ == "__main__":
    main()