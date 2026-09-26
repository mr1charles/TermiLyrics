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

Slowed / sped-up audio: chromaprint fingerprints are not tempo-invariant,
so a "slowed + reverb" upload never matches the original recording. When
ffmpeg is available, a sample that doesn't match as-is is resampled by a
few typical edit factors (asetrate, which undoes a resample-style edit —
tempo *and* pitch) and looked up again. The factor that matches tells us
the track's speed, which the app then uses to re-time the lyrics.

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
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, List, Optional, Sequence, Tuple

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


def _resample(src: Path, dst: Path, factor: float) -> bool:
    """Play `src` `factor` times faster (tempo and pitch together, i.e.
    undo a resample-style slowed/sped-up edit) into `dst`."""
    if not shutil.which("ffmpeg"):
        return False
    try:
        with wave.open(str(src), "rb") as w:
            rate = w.getframerate()
    except (wave.Error, OSError, EOFError):
        rate = 44100
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
           "-af", f"asetrate={int(rate * factor)},aresample={rate}", str(dst)]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except (subprocess.TimeoutExpired, OSError) as e:
        log.debug("ffmpeg resample x%.2f failed: %s", factor, e)
        return False
    return dst.exists() and dst.stat().st_size > 1000


def tempo_attempt_order(factors: Sequence[float], variant: str = "") -> List[float]:
    """1.0 first, then the correction factors most likely for this track:
    a slowed track needs speeding up (factor > 1) and vice versa."""
    rest = [f for f in dict.fromkeys(factors) if f > 0 and abs(f - 1.0) > 1e-6]
    if variant == "slowed":
        rest.sort(key=lambda f: (f < 1.0,))
    elif variant == "sped_up":
        rest.sort(key=lambda f: (f > 1.0,))
    return [1.0] + rest


def _lookup_acoustid(fingerprint: str, duration: int, api_key: str, timeout: float) -> Optional[Tuple[str, str]]:
    match = _lookup_acoustid_full(fingerprint, duration, api_key, timeout)
    return (match[0], match[1]) if match else None


def _lookup_acoustid_full(fingerprint: str, duration: int, api_key: str,
                          timeout: float) -> Optional[Tuple[str, str, Optional[float]]]:
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
        if r.get("score", 0) < 0.4:
            break
        for rec in r.get("recordings") or []:
            title = rec.get("title")
            artists = rec.get("artists") or []
            if title and artists and artists[0].get("name"):
                length = rec.get("duration")
                return artists[0]["name"], title, float(length) if length else None
    return None


@dataclass(frozen=True)
class AudioMatch:
    song: "Song"
    # Length of the matched original recording, if AcoustID knows it.
    duration: Optional[float]
    # Speed of what's playing relative to that recording: 0.8 means the
    # track is a 0.8x slowed edit. 1.0 for a straight match.
    speed: float = 1.0


class AudioIdentifier:
    """Records a short sample of whatever's currently playing, fingerprints
    it locally, and looks it up via AcoustID. Availability (all three of:
    API key configured, fpcalc installed, a recorder installed) is checked
    once and cached."""

    def __init__(self, api_key: str, sample_seconds: float = 15.0,
                 lookup_timeout: float = 6.0, tmp_dir: Optional[Path] = None,
                 tempo_factors: Iterable[float] = ()):
        self.api_key = api_key
        self.sample_seconds = sample_seconds
        self.lookup_timeout = lookup_timeout
        self.tmp_dir = tmp_dir or Path(tempfile.gettempdir())
        self.tempo_factors = tuple(tempo_factors)
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

    def identify(self, variant: str = "") -> Optional[AudioMatch]:
        """Record, fingerprint, look up. `variant` ("slowed"/"sped_up", from
        the title) only reorders which speed corrections are tried first."""
        if not self.available():
            return None
        from .detect import Song  # local import: avoids a module-load-time cycle with detect.py

        path = self.tmp_dir / f"lyrics_sync_fp_{os.getpid()}.wav"
        alt = self.tmp_dir / f"lyrics_sync_fp_{os.getpid()}_tempo.wav"
        try:
            if not _record_sample(path, self.sample_seconds):
                return None
            factors = tempo_attempt_order(self.tempo_factors, variant) if shutil.which("ffmpeg") else [1.0]
            for n, factor in enumerate(factors):
                src = path
                if factor != 1.0:
                    if not _resample(path, alt, factor):
                        continue
                    src = alt
                fp = _fingerprint(src)
                if not fp:
                    continue
                if n:
                    time.sleep(0.34)  # AcoustID allows 3 requests/second
                fingerprint, duration = fp
                result = _lookup_acoustid_full(fingerprint, duration, self.api_key, self.lookup_timeout)
                if result:
                    artist, title, length = result
                    # A confirmed fingerprint match is strong evidence — high confidence.
                    speed = 1.0 / factor
                    log.info("fingerprint match at x%.2f correction: %s - %s", factor, artist, title)
                    song = Song(artist=artist, title=title, artist_confidence="high",
                                variant="" if factor == 1.0 else ("slowed" if speed < 1 else "sped_up"),
                                speed_hint=None if factor == 1.0 else round(speed, 3))
                    return AudioMatch(song=song, duration=length, speed=speed)
            return None
        finally:
            for p in (path, alt):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
