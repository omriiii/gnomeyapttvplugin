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
import math
import os
import queue
import re
import socket
import threading
from urllib.parse import quote_plus

import numpy as np
import requests
import sounddevice as sd
import soundfile as sf

from hlalyx_queries import HLAlyxQueries
from gnome_spatial import GnomeTracker

CHANNEL = "HalDoodlin"
NICK = "justinfan12345"
HOST = "irc.chat.twitch.tv"
PORT = 6667

# Directory where generated .wav files will be saved
OUTPUT_DIR = r"C:\Path\To\Your\Output\Directory"

# HL:A VConsole2 socket (see hlalyx_console.py / hlalyx_queries.py)
HLA_HOST = "127.0.0.1"
HLA_PORT = 29000

# Audio spatialization tuning
#
# Source engine's classic scale is ~1 unit = 1 inch (12 units/foot). Half-Life:
# Alyx is Source 2 and VR scale can differ slightly from that mnemonic -- if
# these gain points feel off in-game, recalibrate UNITS_PER_FOOT empirically:
# stand at a known real-world distance from the gnome and check the
# `distance` value gnome_tracker.py reports for that spot.
UNITS_PER_FOOT = 12.0

NEAR_GAIN = 3.0                          # gain when the gnome is right in your face (distance ~= 0)
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
    # ... rest of your VOICES list
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


def getTTSAudio(text: str, voice_idx: int, pitch: int = 140, speed: int = 150):
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

            print(line)
            match = re.match(
                r"^:(\w+)!\w+@\w+\.tmi\.twitch\.tv PRIVMSG #\w+ :(.+)$", line
            )
            if match:
                username, message = match.groups()
                on_message(username, message)


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

    connect_and_listen()