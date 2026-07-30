"""
tts_cache.py

Small cache so the same (text, voice) combination never round-trips to
the TTS API twice.

Design: the filename is a deterministic hash of `voice_idx + text`, so
"is this cached?" is just "does that file already exist" -- no separate
cache index/database to keep in sync with what's actually on disk. An
in-memory dict sits in front of that just to avoid a stat() call on every
repeat line, and to know exactly which files this cache is responsible
for.

That last part is what clear_and_delete() uses: it's called when HL:A
closes (see twitch_gnome_bot.py's on_status_change handler) to delete
every file this run has generated or reused, and forget them, so a
new game session starts from a clean slate.
"""

import hashlib
import os
import threading
from typing import Optional


class TTSCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._paths: dict = {}  # (text, voice_idx) -> filepath

    @staticmethod
    def filename_for(text: str, voice_idx: int) -> str:
        digest = hashlib.sha256(f"{voice_idx}:{text}".encode("utf-8")).hexdigest()
        return digest + ".wav"

    def get(self, text: str, voice_idx: int, output_dir: str) -> Optional[str]:
        """
        Returns a path to an already-generated file for this (text,
        voice_idx), or None if it needs to be generated. Checks the
        in-memory dict first; if that misses, falls back to checking disk
        directly (filename is deterministic), since the file may already
        be there from earlier in this run even if this particular
        (text, voice_idx) hasn't been looked up before -- e.g. the caller
        restarted the bot without HL:A closing in between.
        """
        key = (text, voice_idx)
        with self._lock:
            path = self._paths.get(key)
        if path and os.path.isfile(path):
            return path

        candidate = os.path.join(output_dir, self.filename_for(text, voice_idx))
        if os.path.isfile(candidate):
            with self._lock:
                self._paths[key] = candidate
            return candidate
        return None

    def put(self, text: str, voice_idx: int, path: str):
        with self._lock:
            self._paths[(text, voice_idx)] = path

    def clear_and_delete(self) -> int:
        """
        Deletes every file this cache currently knows about and forgets
        them all. Returns how many files were actually removed (so the
        caller can log something useful).
        """
        with self._lock:
            paths = list(self._paths.values())
            self._paths.clear()
        removed = 0
        for path in paths:
            try:
                os.remove(path)
                removed += 1
            except OSError:
                pass  # already gone, or in use -- nothing more we can do
        return removed
