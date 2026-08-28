"""Last-resort song identification via audio fingerprinting, for content
with no usable text clue anywhere (title, artist, uploader) — e.g. a vlog
or gameplay video with a song playing in the background, where no amount of
metadata parsing can ever find it.

This is a genuinely different privacy/network posture than the rest of the
app: fingerprinting itself is 100% local (chromaprint/fpcalc, plain DSP, not
AI), but identifying *which song* a fingerprint belongs to requires looking
it up against a database no local machine can realistically hold — so this
sends a compact fingerprint (not raw audio) to AcoustID's free public API.
Off by default; only activates if the user has supplied their own API key.

Requires two external binaries, neither of which are Python packages:
  fpcalc     — from the `chromaprint` package (computes the fingerprint)
  parecord   — from `pulseaudio-utils`/`pipewire-pulse` (records a sample of
               whatever's currently playing, via the default monitor source)
  ffmpeg     — used instead of parecord if that's what's available

Entirely optional and fails safe at every step: if any dependency is
missing, if recording fails, if fpcalc fails, or if the network lookup
fails or times out, `identify()` returns None and the caller keeps
whatever it already had.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Tuple

if TYPE_CHECKING:
    from .detect import Song

log = logging.getLogger(__name__)

ACOUSTID_LOOKUP_URL = "https://api.acoustid.org/v2/lookup"


def _record_sample(path: Path, seconds: float) -> bool:
    """Records `seconds` of whatever's currently playing on the system's
    default audio output (its monitor source, not the microphone)."""
    if shutil.which("parecord"):
        cmd = ["timeout", str(seconds + 1), "parecord",
               "--device=@DEFAULT_MONITOR@", "--file-format=wav", str(path)]
    elif shutil.which("ffmpeg"):
        cmd = ["ffmpeg", "-y", "-f", "pulse", "-i", "default", "-t", str(seconds), str(path)]
    else:
        return False
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=seconds + 5)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("audio sample recording failed: %s", e)
        return False
    return path.exists() and path.stat().st_size > 1000


def _fingerprint(path: Path) -> Optional[Tuple[str, int]]:
    if not shutil.which("fpcalc"):
        return None
    try:
        out = subprocess.check_output(["fpcalc", "-json", str(path)], stderr=subprocess.DEVNULL, timeout=15)
        data = json.loads(out.decode("utf-8"))
        return data["fingerprint"], int(data["duration"])
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError,
            json.JSONDecodeError, KeyError, ValueError) as e:
        log.debug("fpcalc failed: %s", e)
        return None


def _lookup_acoustid(fingerprint: str, duration: int, api_key: str, timeout: float) -> Optional[Tuple[str, str]]:
    params = urllib.parse.urlencode({
        "client": api_key,
        "format": "json",
        "duration": duration,
        "fingerprint": fingerprint,
        "meta": "recordings",
    })
    url = f"{ACOUSTID_LOOKUP_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as e:
        log.debug("AcoustID lookup failed: %s", e)
        return None

    if data.get("status") != "ok":
        return None
    results = sorted(data.get("results", []), key=lambda r: r.get("score", 0), reverse=True)
    for r in results:
        for rec in r.get("recordings") or []:
            title = rec.get("title")
            artists = rec.get("artists") or []
            if title and artists and artists[0].get("name"):
                return artists[0]["name"], title
    return None


class AudioIdentifier:
    """Records a short sample of whatever's currently playing, fingerprints
    it locally, and looks it up via AcoustID. Availability (all three of:
    API key configured, fpcalc installed, a recorder installed) is checked
    once and cached."""

    def __init__(self, api_key: str, sample_seconds: float = 8.0,
                 lookup_timeout: float = 6.0, tmp_dir: Optional[Path] = None):
        self.api_key = api_key
        self.sample_seconds = sample_seconds
        self.lookup_timeout = lookup_timeout
        self.tmp_dir = tmp_dir or Path(tempfile.gettempdir())
        self._checked = False
        self._available = False

    def available(self) -> bool:
        if not self._checked:
            self._checked = True
            has_recorder = bool(shutil.which("parecord")) or bool(shutil.which("ffmpeg"))
            self._available = bool(self.api_key) and bool(shutil.which("fpcalc")) and has_recorder
            if not self._available:
                log.info(
                    "Audio fingerprint fallback unavailable (api_key=%s, fpcalc=%s, recorder=%s)",
                    bool(self.api_key), bool(shutil.which("fpcalc")), has_recorder,
                )
        return self._available

    def identify(self) -> Optional["Song"]:
        if not self.available():
            return None
        from .detect import Song  # local import: avoids a module-load-time cycle with detect.py

        path = self.tmp_dir / f"lyrics_sync_fp_{os.getpid()}.wav"
        try:
            if not _record_sample(path, self.sample_seconds):
                return None
            fp = _fingerprint(path)
            if not fp:
                return None
            fingerprint, duration = fp
            result = _lookup_acoustid(fingerprint, duration, self.api_key, self.lookup_timeout)
            if not result:
                return None
            artist, title = result
            # A confirmed fingerprint match is strong evidence — high confidence.
            return Song(artist=artist, title=title, artist_confidence="high")
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
