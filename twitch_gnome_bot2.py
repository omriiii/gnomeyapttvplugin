"""
twitch_gnome_bot.py

Your Twitch IRC + TTS code, wired up to play each generated clip "from the
gnome": panned left/right and attenuated by distance based on the gnome's
position relative to the player in-game, continuously re-sampled while the
clip plays so it slides smoothly instead of snapping.

How the 3D illusion is built (there's no real audio engine involved, just
math on a stereo signal):
  - bearing (left/right of where Alyx is looking) -> constant-power stereo
    pan. bearing=0 is centered, +90 is hard right, -90 is hard left.
  - distance -> linear volume falloff, floored so it never goes silent.
  - elevation is tracked (see gnome_spatial.relative_position) but isn't
    used for the audio yet -- stereo panning can't convey up/down. Worth
    revisiting with an HRTF library if you want that later.
  - Every audio chunk (~20-30ms) re-queries GnomeTracker.get_relative_now(),
    which interpolates between the gnome's last two known positions using
    wall-clock time. That's what gives the smooth slide instead of a jump
    each time a new poll sample (every poll_interval seconds) comes in.

New dependencies beyond your original script:
    pip install sounddevice soundfile numpy
"""

import hashlib
import json
import math
import os
import queue
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import quote_plus, urlparse, parse_qs

import numpy as np
import requests
import sounddevice as sd
import soundfile as sf

from hlalyx_queries import HLAlyxQueries
from gnome_spatial import GnomeTracker

CHANNEL = "omrii_"
NICK = "justinfan12345"
HOST = "irc.chat.twitch.tv"
PORT = 6667

# Directory where generated .wav files will be saved
OUTPUT_DIR = r"C:\Path\To\Your\Output\Directory"

# HL:A VConsole2 socket (see hlalyx_console.py / hlalyx_queries.py)
HLA_HOST = "127.0.0.1"
HLA_PORT = 29000

# --------------------------------------------------------------------- #
# Redeem gating
#
# Plain anonymous IRC (justinfan...) does NOT see channel point
# redemptions as chat messages -- they aren't sent as PRIVMSG at all,
# except for the one edge case where a redeem requires the viewer to type
# text, which gets tagged with a custom-reward-id IRC tag. Relying on that
# would mean looking up the reward's UUID via the authenticated Twitch
# Helix API, which is more moving parts than necessary here.
#
# Since Streamer.bot is already handling Twitch auth, it has a native
# "Reward Redemption" trigger that can filter by reward name directly.
# When ONLY_FROM_REDEEM is True, this bot ignores normal Twitch chat
# entirely and only speaks when Streamer.bot POSTs to the local HTTP
# listener below (see the Streamer.bot setup notes near run_redeem_server).
ONLY_FROM_REDEEM = True
REDEEM_NAME = "Gnome Yap"          # just for logging clarity
REDEEM_LISTEN_HOST = "127.0.0.1"
REDEEM_LISTEN_PORT = 3939
REDEEM_PATH = "/gnome_yap"

# Audio spatialization tuning
#
# Source engine's classic scale is ~1 unit = 1 inch (12 units/foot). Half-Life:
# Alyx is Source 2 and VR scale can differ slightly from that mnemonic -- if
# these gain points feel off in-game, recalibrate UNITS_PER_FOOT empirically:
# stand at a known real-world distance from the gnome and check the
# `distance` value gnome_tracker.py reports for that spot.
UNITS_PER_FOOT = 12.0

NEAR_GAIN = 2.0                          # gain when the gnome is right in your face (distance ~= 0)
REF_DISTANCE_FEET = 20.0
REF_GAIN = 0.5                           # gain at REF_DISTANCE_FEET away
REF_DISTANCE_UNITS = REF_DISTANCE_FEET * UNITS_PER_FOOT

MIN_DISTANCE_GAIN = 0.05                 # never fully silent far away
MAX_DISTANCE_GAIN = NEAR_GAIN            # cap -- shouldn't need to exceed the point-blank gain

CHUNK_FRAMES = 1024             # ~23ms at 44.1kHz; smaller = smoother panning

VOICES = [
    "Adult Female #1, American English (TruVoice)",
    "Adult Female #2, American English (TruVoice)",
    "Adult Male #1, American English (TruVoice)",
    "Adult Male #2, American English (TruVoice)",
    "Adult Male #3, American English (TruVoice)",
    "Adult Male #4, American English (TruVoice)",
    "Adult Male #5, American English (TruVoice)",
    "Adult Male #6, American English (TruVoice)",
    "Adult Male #7, American English (TruVoice)",
    "Adult Male #8, American English (TruVoice)",
    "Brutus",
    "Chinese-Simplified: Li3 Dong1dong1 (Child), 6.1",
    "Chinese-Simplified: Li3 Jin4 (Adult Male), 6.1",
    "Chinese-Simplified: Li3 Jin4-Tel (Adult Male for Telephone), 6.1",
    "Chinese-Simplified: Nai3nai0 (Elderly Female), 6.1",
    "Chinese-Simplified: Wang2 Yan4 (Adult Female), 6.1",
    "Chinese-Simplified: Wang2 Yan4-Tel (Adult Female for Telephone), 6.1",
    "Chinese-Simplified: Ye2ye0 (Elderly Male), 6.1",
    "English-American: Grandma (Elderly Female), 6.1",
    "English-American: Grandpa (Elderly Male), 6.1",
    "English-American: Reed (Adult Male), 6.1",
    "English-American: Reed-Tel (Adult Male for Telephone), 6.1",
    "English-American: Sandy (Child), 6.1",
    "English-American: Shelley (Adult Female), 6.1",
    "English-American: Shelley-Tel (Adult Female for Telephone), 6.1",
    "English-British: Gramps (Elderly Male), 6.1",
    "English-British: Jane (Adult Female), 6.1",
    "English-British: Jane-Tel (Adult Female for Telephone), 6.1",
    "English-British: Justin (Adult Male), 6.1",
    "English-British: Justin-Tel (Adult Male for Telephone), 6.1",
    "English-British: Nanny (Elderly Female), 6.1",
    "English-British: Nicky (Child), 6.1",
    "Female Whisper",
    "Finnish-Standard: Antti (Adult Male), 6.1",
    "Finnish-Standard: Antti-Tel (Adult Male for Telephone), 6.1",
    "Finnish-Standard: Isoisä (Elderly Male), 6.1",
    "Finnish-Standard: Isoäiti (Elderly Female), 6.1",
    "Finnish-Standard: Pekka (Child), 6.1",
    "Finnish-Standard: Tarja (Adult Female), 6.1",
    "Finnish-Standard: Tarja-Tel (Adult Female for Telephone), 6.1",
    "Freddy",
    "French-Canadian: Daniel (Adult Male), 6.1",
    "French-Canadian: Daniel-Tel (Adult Male for Telephone), 6.1",
    "French-Canadian: Denis (Child), 6.1",
    "French-Canadian: Grand-maman (Elderly Female), 6.1",
    "French-Canadian: Grand-papa (Elderly Male), 6.1",
    "French-Canadian: Nicole (Adult Female), 6.1",
    "French-Canadian: Nicole-Tel (Adult Female for Telephone), 6.1",
    "French-Standard: Grand-mère (Elderly Female), 6.1",
    "French-Standard: Grand-père (Elderly Male), 6.1",
    "French-Standard: Jacqueline (Adult Female), 6.1",
    "French-Standard: Jacqueline-Tel (Adult Female for Telephone), 6.1",
    "French-Standard: Jacques (Adult Male), 6.1",
    "French-Standard: Jacques-Tel (Adult Male for Telephone), 6.1",
    "French-Standard: Marius (Child), 6.1",
    "German-Standard: Gisela (Adult Female), 6.1",
    "German-Standard: Gisela-Tel (Adult Female for Telephone), 6.1",
    "German-Standard: Matti (Child), 6.1",
    "German-Standard: Max (Adult Male), 6.1",
    "German-Standard: Max-Tel (Adult Male for Telephone), 6.1",
    "German-Standard: Oma (Elderly Female), 6.1",
    "German-Standard: Opa (Elderly Male), 6.1",
    "Italian-Standard: Chicco (Child), 6.1",
    "Italian-Standard: Enrico (Adult Male), 6.1",
    "Italian-Standard: Enrico-Tel (Adult Male for Telephone), 6.1",
    "Italian-Standard: Lucia (Adult Female), 6.1",
    "Italian-Standard: Lucia-Tel (Adult Female for Telephone), 6.1",
    "Italian-Standard: Nonna (Elderly Female), 6.1",
    "Italian-Standard: Nonno (Elderly Male), 6.1",
    "Japanese-Standard: Hanako (Adult Female), 6.1",
    "Japanese-Standard: Hanako-Tel (Adult Female for Telephone), 6.1",
    "Japanese-Standard: Jiroo (Child), 6.1",
    "Japanese-Standard: Obaachan (Elderly Female), 6.1",
    "Japanese-Standard: Ojiisan (Elderly Male), 6.1",
    "Japanese-Standard: Taroo (Adult Male), 6.1",
    "Japanese-Standard: Taroo-Tel (Adult Male for Telephone), 6.1",
    "Korean-Standard: haLapEci (Elderly Male), 6.1",
    "Korean-Standard: haLmEni (Elderly Female), 6.1",
    "Korean-Standard: haNkinaM (Adult Male), 6.1",
    "Korean-Standard: haNkinaM-Tel (Adult Male for Telephone), 6.1",
    "Korean-Standard: haNkiraN (Adult Female), 6.1",
    "Korean-Standard: haNkiraN-Tel (Adult Female for Telephone), 6.1",
    "Korean-Standard: haNkitoNG (Child), 6.1",
    "Male Whisper",
    "Mary",
    "Mary (for Telephone)",
    "Mary in Hall",
    "Mary in Space",
    "Mary in Stadium",
    "Mike",
    "Mike (for Telephone)",
    "Mike in Hall",
    "Mike in Space",
    "Mike in Stadium",
    "Portuguese-Brazilian: Avó (Elderly Female), 6.1",
    "Portuguese-Brazilian: Avô (Elderly Male), 6.1",
    "Portuguese-Brazilian: Chico (Child), 6.1",
    "Portuguese-Brazilian: Cláudia (Adult Female), 6.1",
    "Portuguese-Brazilian: Cláudia-Tel (Adult Female for Telephone), 6.1",
    "Portuguese-Brazilian: João (Adult Male), 6.1",
    "Portuguese-Brazilian: João-Tel (Adult Male for Telephone), 6.1",
    "RoboSoft Five",
    "RoboSoft Four",
    "RoboSoft One",
    "RoboSoft Six",
    "RoboSoft Three",
    "RoboSoft Two",
    "Sam",
    "Spanish-Castilian: Abuela (Elderly Female), 6.1",
    "Spanish-Castilian: Abuelo (Elderly Male), 6.1",
    "Spanish-Castilian: Carlos (Adult Male), 6.1",
    "Spanish-Castilian: Carlos-Tel (Adult Male for Telephone), 6.1",
    "Spanish-Castilian: Pepe (Child), 6.1",
    "Spanish-Castilian: Pilar (Adult Female), 6.1",
    "Spanish-Castilian: Pilar-Tel (Adult Female for Telephone), 6.1",
    "Spanish-Mexican: Abuelita (Elderly Female), 6.1",
    "Spanish-Mexican: Abuelito (Elderly Male), 6.1",
    "Spanish-Mexican: José (Adult Male), 6.1",
    "Spanish-Mexican: José-Tel (Adult Male for Telephone), 6.1",
    "Spanish-Mexican: Marisol (Adult Female), 6.1",
    "Spanish-Mexican: Marisol-Tel (Adult Female for Telephone), 6.1",
    "Spanish-Mexican: Panchito (Child), 6.1",
]

user_voices = {}  # username -> voice_idx


def get_voice_for_user(username: str) -> int:
    if username in user_voices:
        return user_voices[username]
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()
    voice_idx = int(digest, 16) % len(VOICES)
    user_voices[username] = voice_idx
    print(f"Gave {username} voice {voice_idx}")
    return voice_idx


def set_voice_for_user(username: str, requested_index: int) -> int:
    """
    Explicitly sets username's voice from a !gnomevoice <int> chat command.
    Wraps requested_index into range via modulo against however many VOICES
    currently exist, so any integer (including ones bigger than the list or
    negative) always lands on a valid voice instead of erroring.
    """
    voice_idx = requested_index % len(VOICES)
    user_voices[username] = voice_idx
    print(f"[gnomevoice] {username} set their voice to #{voice_idx}: {VOICES[voice_idx]}")
    return voice_idx


def getTTSAudio(text: str, voice_idx: int, pitch: int = 130, speed: int = 150):
    """Fetch TTS audio and save it as a .wav file in OUTPUT_DIR. Returns the full path or None."""
    encoded_text = quote_plus(text)
    voice = quote_plus(VOICES[voice_idx])
    tts_url = f"https://www.tetyys.com/SAPI4/SAPI4?text={encoded_text}&voice={voice}&pitch={pitch}&speed={speed}"

    fname = hashlib.sha256(text.encode("utf-8")).hexdigest() + ".wav"
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    full_wav_path = os.path.join(OUTPUT_DIR, fname)

    try:
        response = requests.get(tts_url)
        if response.status_code != 200:
            print("Failed to fetch audio!")
            return None
        with open(full_wav_path, "wb") as f:
            f.write(response.content)
    except Exception as e:
        print(f"Failed to fetch audio: {e}")
        return None

    return full_wav_path


# ---------------------------------------------------------------------- #
# Positional playback
# ---------------------------------------------------------------------- #
def distance_gain(distance: float) -> float:
    """
    Linear gain curve calibrated to two points: NEAR_GAIN at distance=0,
    REF_GAIN at REF_DISTANCE_UNITS (20 feet). Continues at the same slope
    beyond that (getting quieter further out), floored/capped so it never
    goes silent or blows past MAX_DISTANCE_GAIN.
    """
    slope = (REF_GAIN - NEAR_GAIN) / REF_DISTANCE_UNITS
    gain = NEAR_GAIN + slope * distance
    return max(MIN_DISTANCE_GAIN, min(MAX_DISTANCE_GAIN, gain))


def pan_and_distance_gains(bearing_deg: float, distance: float):
    """Constant-power stereo pan from bearing, times the calibrated distance gain."""
    pan = math.sin(math.radians(bearing_deg))  # -1 (hard left) .. +1 (hard right)
    left_gain = math.sqrt(0.5 * (1.0 - pan))
    right_gain = math.sqrt(0.5 * (1.0 + pan))

    dgain = distance_gain(distance)
    return left_gain * dgain, right_gain * dgain


def play_spatial(path: str, tracker: GnomeTracker):
    """
    Streams `path` to the default output device, re-panning/attenuating
    every CHUNK_FRAMES based on the gnome's current interpolated position
    relative to the player.
    """
    data, samplerate = sf.read(path, dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)  # downmix source to mono; we re-pan to stereo ourselves

    stream = sd.OutputStream(samplerate=samplerate, channels=2, dtype="float32")
    stream.start()
    try:
        for start in range(0, len(data), CHUNK_FRAMES):
            chunk = data[start:start + CHUNK_FRAMES]

            rel = tracker.get_relative_now()
            if rel is not None:
                distance, bearing, _elevation = rel
                left_gain, right_gain = pan_and_distance_gains(bearing, distance)
            else:
                # No position data yet -- fall back to centered, full volume
                # rather than silence, so the bot still works before the
                # game/tracker connection is up.
                left_gain = right_gain = 1.0

            stereo_chunk = np.empty((len(chunk), 2), dtype="float32")
            stereo_chunk[:, 0] = chunk * left_gain
            stereo_chunk[:, 1] = chunk * right_gain
            # NEAR_GAIN can push samples past +-1.0 when the gnome is right
            # up on you -- clip here to avoid audible distortion/wraparound.
            np.clip(stereo_chunk, -1.0, 1.0, out=stereo_chunk)
            stream.write(stereo_chunk)
    finally:
        stream.stop()
        stream.close()


# One playback at a time, sequential, so overlapping chat messages don't
# fight over the audio device or talk over each other.
_playback_queue: "queue.Queue[str]" = queue.Queue()


def _playback_worker(tracker: GnomeTracker):
    while True:
        path = _playback_queue.get()
        try:
            play_spatial(path, tracker)
        except Exception as e:
            print(f"Playback failed for {path}: {e}")
        finally:
            _playback_queue.task_done()


# ---------------------------------------------------------------------- #
# Twitch chat
# ---------------------------------------------------------------------- #
def on_message(username: str, message: str):
    print(f"{username}: {message}")
    voice_idx = get_voice_for_user(username)

    saved_path = getTTSAudio(message.lower(), voice_idx=voice_idx)
    if saved_path:
        print(f"Saved TTS audio to: {saved_path}")
        _playback_queue.put(saved_path)


# Matches "!gnomevoice <integer>", e.g. "!gnomevoice 3" or "!gnomevoice -1"
GNOME_VOICE_COMMAND_RE = re.compile(r"^!gnomevoice\s+(-?\d+)\s*$", re.IGNORECASE)
# Catches a mistyped/incomplete attempt at the command, so it gets quietly
# dropped instead of accidentally getting read aloud as TTS.
GNOME_VOICE_PREFIX_RE = re.compile(r"^!gnomevoice\b", re.IGNORECASE)


def handle_chat_line(username: str, message: str):
    """
    Entry point for every real Twitch chat line (as opposed to a redeem).
    Always checks for the !gnomevoice command first -- viewers can set
    their voice from chat regardless of whether ONLY_FROM_REDEEM is gating
    actual TTS playback to the "Gnome Yap" redeem. Note: this bot connects
    as an anonymous justinfan... user, which is read-only, so it can't post
    a confirmation back to chat -- confirmations only show up in this
    script's own console output.
    """
    stripped = message.strip()

    match = GNOME_VOICE_COMMAND_RE.match(stripped)
    if match:
        set_voice_for_user(username, int(match.group(1)))
        return

    if GNOME_VOICE_PREFIX_RE.match(stripped):
        print(f"[gnomevoice] Ignoring malformed command from {username}: {message!r} "
              f"(expected: !gnomevoice <integer>)")
        return

    if not ONLY_FROM_REDEEM:
        on_message(username, message)
    # else: ONLY_FROM_REDEEM is True -- normal chat doesn't trigger TTS,
    # but the !gnomevoice command above still works either way.


class _RedeemRequestHandler(BaseHTTPRequestHandler):
    """
    Handles requests from Streamer.bot's "Gnome Yap" redemption trigger.

    Supports BOTH:
      - POST with a JSON body: {"username": "...", "message": "..."}
        (this is what the provided Streamer.bot C# sub-action sends --
        use this one, it avoids URL-encoding issues entirely)
      - GET with query params: /gnome_yap?username=...&message=...
        (kept for quick manual testing from a browser/curl)

    `message` should be whatever text the viewer typed into the redeem (if
    it requires text input). If your redeem doesn't collect text, leave it
    blank/omit it and a fallback phrase is used instead.
    """

    def do_POST(self):
        if self.path != REDEEM_PATH:
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            self.send_response(400)
            self.end_headers()
            return

        username = str(payload.get("username", "")).strip() or "anonymous"
        message = str(payload.get("message", "")).strip() or "gnome yap!"

        on_message(username, message)

        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != REDEEM_PATH:
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        username = (params.get("username", [""])[0]).strip() or "anonymous"
        message = (params.get("message", [""])[0]).strip() or "gnome yap!"

        on_message(username, message)

        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        pass  # silence BaseHTTPRequestHandler's default per-request stdout logging


def run_redeem_server():
    """
    Runs the local HTTP listener that Streamer.bot talks to.

    Streamer.bot setup:
      1. Actions -> New Action (e.g. "Gnome Yap Redeem")
      2. Add Trigger -> Twitch -> Channel Points -> Reward Redemption,
         and set it to filter on the reward named "Gnome Yap"
         (Streamer.bot lets you pick/filter by reward name directly --
         no reward UUID lookup needed).
      3. Add a sub-action -> Core -> Execute C# Code -> paste in
         GnomeYapRedeem.cs. It reads the "user"/"rawInput" trigger
         arguments and POSTs them as JSON to:
             http://127.0.0.1:3939/gnome_yap
         This is the path to use -- it sidesteps URL-encoding issues
         with punctuation/spaces in chat text entirely.
      4. Save, then test-redeem it with both this script and
         Streamer.bot running.

    (For quick manual testing without Streamer.bot, GET also works:
    http://127.0.0.1:3939/gnome_yap?username=foo&message=hello)
    """
    server = HTTPServer((REDEEM_LISTEN_HOST, REDEEM_LISTEN_PORT), _RedeemRequestHandler)
    print(f"Listening for '{REDEEM_NAME}' redemptions on "
          f"http://{REDEEM_LISTEN_HOST}:{REDEEM_LISTEN_PORT}{REDEEM_PATH}")
    server.serve_forever()


def connect_and_listen():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((HOST, PORT))

    sock.send(f"NICK {NICK}\r\n".encode("utf-8"))
    sock.send(f"JOIN #{CHANNEL}\r\n".encode("utf-8"))

    buffer = ""
    while True:
        buffer += sock.recv(2048).decode("utf-8", errors="ignore")
        lines = buffer.split("\r\n")
        buffer = lines.pop()

        for line in lines:
            if line.startswith("PING"):
                sock.send("PONG :tmi.twitch.tv\r\n".encode("utf-8"))
                continue

            match = re.match(
                r"^:(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.+)$", line
            )
            if match:
                username, message = match.groups()
                handle_chat_line(username, message)


if __name__ == "__main__":
    # verbose=False keeps VConsole output out of stdout (perf + noise).
    # debug_timing=True logs getpos/print_ents round-trip latency -- flip
    # this on if you want to see the numbers; see hlalyx_latency_check.py
    # for a standalone version that doesn't require the game to be up to
    # try it out.
    hla_queries = HLAlyxQueries(host=HLA_HOST, port=HLA_PORT,
                                 verbose=False, debug_timing=False)
    hla_queries.connect()

    # Player pose polls fast (drives audio panning on head-turns); gnome
    # position polls slower since it moves far less than your head does.
    tracker = GnomeTracker(hla_queries, player_poll_interval=0.05, gnome_poll_interval=0.25)
    tracker.start()

    threading.Thread(target=_playback_worker, args=(tracker,), daemon=True).start()

    # Chat is always connected so viewers can use !gnomevoice, regardless
    # of whether ONLY_FROM_REDEEM is limiting actual TTS playback to the
    # "Gnome Yap" redeem. handle_chat_line() is what decides, per message,
    # whether it's a voice command or (if not gated) a TTS trigger.
    threading.Thread(target=connect_and_listen, daemon=True).start()

    if ONLY_FROM_REDEEM:
        threading.Thread(target=run_redeem_server, daemon=True).start()
        print(f"ONLY_FROM_REDEEM is True: TTS only fires from '{REDEEM_NAME}' "
              f"redemptions via Streamer.bot. (!gnomevoice still works from chat.)")
    else:
        print("Listening to Twitch chat for both TTS and !gnomevoice.")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopped.")