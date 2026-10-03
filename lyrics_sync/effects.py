"""Artist-specific visual effects, driven by the beat of whatever is playing.

Three pieces:

  * ARTIST_FX / fx_for_artist() — which artists get which effect and palette
    (BLACKPINK gets pink stage lights, ...). Add your own in
    ~/Lyrics-Sync/artist_fx.json, e.g.
        {"Daft Punk": {"effect": "stage_lights", "colors": ["#00e5ff", "#ff2bd6"]}}
  * BeatDetector / BeatTracker — a dependency-free kick-drum detector. The
    tracker listens to the system audio output ("monitor" of the default sink,
    through `parec`, which both PulseAudio and PipeWire provide). Nothing is
    recorded or stored: samples are analysed in memory and thrown away. When
    capture isn't possible the app falls back to pulsing on each lyric line.
  * paint_stage_lights() / paint_confetti() — the effects themselves, painted
    into the canvas *behind* the lyrics.

Flashes are rate-limited (at most ~3 per second, soft colours rather than
white) and everything can be switched off with `x` or --no-fx.
"""
from __future__ import annotations

import array
import json
import logging
import math
import random
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, List, Optional, Sequence, Tuple

from .canvas import Canvas, style
from .themes import ColorTheme, lerp

log = logging.getLogger(__name__)

RGB = Tuple[int, int, int]

EFFECT_STAGE_LIGHTS = "stage_lights"
EFFECT_CONFETTI = "confetti"
EFFECTS = (EFFECT_STAGE_LIGHTS, EFFECT_CONFETTI)


# --------------------------------------------------------------------------
# Who gets what
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ArtistFx:
    name: str
    effect: str
    colors: Tuple[RGB, ...]          # beam / particle colors, brightest first

    @property
    def theme(self) -> ColorTheme:
        c = self.colors
        second = c[1] if len(c) > 1 else c[0]
        return ColorTheme(self.name, c[0], second, (255, 255, 255), lerp(c[0], (90, 90, 90), 0.7))


def _hex(code: str) -> RGB:
    code = code.lstrip("#")
    return int(code[0:2], 16), int(code[2:4], 16), int(code[4:6], 16)


def _fx(name: str, effect: str, *colors: str) -> ArtistFx:
    return ArtistFx(name, effect, tuple(_hex(c) for c in colors))


_L = EFFECT_STAGE_LIGHTS
_C = EFFECT_CONFETTI

# key: lowercase letters/digits only (see _norm)
ARTIST_FX: Dict[str, ArtistFx] = {k: v for k, v in {
    "blackpink": _fx("BLACKPINK", _L, "#ff5fa8", "#ff1493", "#ffb6d5"),
    "jisoo": _fx("BLACKPINK", _L, "#ff5fa8", "#ff1493", "#ffb6d5"),
    "jennie": _fx("BLACKPINK", _L, "#ff5fa8", "#ff1493", "#ffb6d5"),
    "rosé": _fx("BLACKPINK", _L, "#ff5fa8", "#ff1493", "#ffb6d5"),
    "lisa": _fx("BLACKPINK", _L, "#ff5fa8", "#ff1493", "#ffb6d5"),
    "bts": _fx("BTS", _L, "#a66cff", "#7d3cff", "#e0c8ff"),
    "twice": _fx("TWICE", _L, "#ff9d57", "#ff3d9a", "#ffd2a8"),
    "nct": _fx("NCT", _L, "#3dff9a", "#00d4ff", "#c8ffe4"),
    "nctwish": _fx("NCT WISH", _L, "#ffe14d", "#ff8fb8", "#b8f0ff"),
    "nct127": _fx("NCT 127", _L, "#3dff9a", "#00d4ff", "#c8ffe4"),
    "nctdream": _fx("NCT DREAM", _L, "#7dff6b", "#3dd6ff", "#e1ffd6"),
    "straykids": _fx("Stray Kids", _L, "#ff3b3b", "#ffffff", "#ff9a9a"),
    "aespa": _fx("aespa", _L, "#9fb4ff", "#e0b8ff", "#6fe3ff"),
    "itzy": _fx("ITZY", _L, "#ff4fa3", "#59e0ff", "#ffe14d"),
    "newjeans": _fx("NewJeans", _L, "#7fc4ff", "#c8e4ff", "#ffb8e0"),
    "lesserafim": _fx("LE SSERAFIM", _L, "#4fa8ff", "#ff6b6b", "#e8d8ff"),
    "ive": _fx("IVE", _L, "#ff5bd1", "#7fb4ff", "#ffd1f3"),
    "gidle": _fx("(G)I-DLE", _L, "#c13dff", "#ff4fb2", "#f0c8ff"),
    "exo": _fx("EXO", _L, "#d9d9ff", "#8f8fff", "#ffffff"),
    "seventeen": _fx("SEVENTEEN", _L, "#f7b8c4", "#8fb8ff", "#ffe0e8"),
    "redvelvet": _fx("Red Velvet", _L, "#ff3355", "#ffb3c1", "#ff8aa0"),
    "txt": _fx("TXT", _L, "#6fd0ff", "#b794ff", "#e0f4ff"),
    "enhypen": _fx("ENHYPEN", _L, "#ff5a36", "#ffffff", "#ffb09a"),
    "ateez": _fx("ATEEZ", _L, "#4ff0ff", "#ffe14d", "#c8faff"),
    "mamamoo": _fx("MAMAMOO", _L, "#ff6fb5", "#ffd24d", "#ffc2e0"),
    "bigbang": _fx("BIGBANG", _L, "#ffd400", "#ffffff", "#ffe97d"),
    "girlsgeneration": _fx("Girls' Generation", _L, "#ff9ad5", "#ffc2e8", "#ffffff"),
    "coldplay": _fx("Coldplay", _C, "#ff4d6d", "#ffd23f", "#3fe0ff", "#7cff6b", "#b57cff"),
    "daftpunk": _fx("Daft Punk", _L, "#00e5ff", "#ff2bd6", "#ffe14d"),
    "billieeilish": _fx("Billie Eilish", _L, "#7dff3a", "#b6ff7d", "#ffffff"),
    "theweeknd": _fx("The Weeknd", _L, "#ff2d2d", "#ff7a7a", "#ffd1d1"),
}.items()}

_SPLIT_RE = re.compile(r"\s*(?:,|;|&|/|\bfeat\.?|\bft\.?|\bx\b|\bwith\b|\band\b)\s*", re.I)


def _norm(name: str) -> str:
    return re.sub(r"[^0-9a-zà-ÿ]", "", (name or "").casefold())


def load_user_fx(path: Optional[Path]) -> Dict[str, ArtistFx]:
    """~/Lyrics-Sync/artist_fx.json — silently ignored when missing/invalid."""
    out: Dict[str, ArtistFx] = {}
    if not path or not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        for artist, spec in data.items():
            colors = tuple(_hex(c) for c in spec.get("colors", [])) or ((255, 255, 255),)
            effect = spec.get("effect", EFFECT_STAGE_LIGHTS)
            if effect in EFFECTS:
                out[_norm(artist)] = ArtistFx(str(artist), effect, colors)
    except (OSError, ValueError, AttributeError, TypeError, IndexError) as e:
        log.warning("ignoring %s: %s", path, e)
    return out


def fx_for_artist(artist: str, extra: Optional[Dict[str, ArtistFx]] = None) -> Optional[ArtistFx]:
    """The effect for a (possibly multi-artist) string like "BLACKPINK, Selena Gomez"."""
    if not artist:
        return None
    table = dict(ARTIST_FX)
    if extra:
        table.update(extra)
    whole = _norm(artist)
    if whole in table:
        return table[whole]
    for part in _SPLIT_RE.split(artist):
        hit = table.get(_norm(part))
        if hit:
            return hit
    return None


# --------------------------------------------------------------------------
# Beat detection
# --------------------------------------------------------------------------

class BeatDetector:
    """Kick-drum onset detector on mono 16-bit samples. Pure Python (no numpy):
    one-pole low-pass (~175 Hz) → short-time energy → onset when the energy
    jumps well above its recent average, with a refractory period so the
    result never flashes faster than ~3.5 times a second."""

    def __init__(self, rate: int = 11025, hop: int = 256, min_gap: float = 0.28, sensitivity: float = 1.0):
        self.rate = rate
        self.hop = hop
        self.min_gap = min_gap
        self.sensitivity = sensitivity
        self._alpha = 1.0 - math.exp(-2.0 * math.pi * 175.0 / rate)
        self._lp = 0.0
        self._env_hist: Deque[float] = deque(maxlen=48)      # ~1.1 s
        self._flux_hist: Deque[float] = deque(maxlen=48)
        self._prev_env = 0.0
        self._samples_seen = 0
        self._last_beat_at = -10.0
        self._pending: List[float] = []

    def feed(self, samples: Sequence[int]) -> List[Tuple[int, float]]:
        """Consume samples. Returns [(sample_index_of_hop_end, strength 0..1)] for each beat."""
        beats: List[Tuple[int, float]] = []
        buf = self._pending
        buf.extend(samples)
        hop, a = self.hop, self._alpha
        pos = 0
        while len(buf) - pos >= hop:
            lp = self._lp
            e = 0.0
            for i in range(pos, pos + hop):
                lp += a * (buf[i] * (1.0 / 32768.0) - lp)
                e += lp * lp
            self._lp = lp
            pos += hop
            self._samples_seen += hop
            e /= hop
            flux = e - self._prev_env
            self._prev_env = e
            hist, fhist = self._env_hist, self._flux_hist
            if len(hist) >= 16:
                avg = sum(hist) / len(hist)
                favg = sum(fhist) / len(fhist)
                t = self._samples_seen / self.rate
                if (flux > 0 and e > 2e-5 and e > (1.0 + 0.30 / self.sensitivity) * avg
                        and flux > (1.8 / self.sensitivity) * favg + 1e-6
                        and t - self._last_beat_at >= self.min_gap):
                    strength = min(1.0, 0.45 + 0.55 * min(e / (avg * 3.0 + 1e-9), 1.0))
                    beats.append((self._samples_seen, strength))
                    self._last_beat_at = t
            hist.append(e)
            fhist.append(max(flux, 0.0))
        del buf[:pos]
        return beats


def _default_monitor_args() -> Optional[List[str]]:
    """Command that streams the system's output as mono s16le @11025 Hz."""
    exe = shutil.which("parec")
    if not exe:
        return None
    return [exe, "--device=@DEFAULT_MONITOR@", "--rate=11025", "--channels=1", "--format=s16le",
            "--latency-msec=30", "--client-name=termilyrics-beat", "--stream-name=beat-detector"]


class BeatTracker:
    """Background thread: system audio → beat times (monotonic seconds)."""

    def __init__(self, offset: float = 0.0, command: Optional[List[str]] = None):
        self.offset = offset
        self._command = command
        self._beats: Deque[Tuple[float, float]] = deque(maxlen=8)    # (time, strength)
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._stop = threading.Event()
        self.available: Optional[bool] = None      # None = not tried yet
        self.last_audio_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="beat-tracker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        proc = self._proc
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def beats(self) -> Tuple[Tuple[float, float], ...]:
        with self._lock:
            return tuple(self._beats)

    def _run(self) -> None:
        cmd = self._command or _default_monitor_args()
        if not cmd:
            self.available = False
            return
        det = BeatDetector()
        try:
            self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        except OSError as e:
            log.info("beat capture unavailable: %s", e)
            self.available = False
            return
        self.available = True
        out = self._proc.stdout
        chunk = det.hop * 2 * 4          # 4 hops of 16-bit samples per read
        try:
            while not self._stop.is_set():
                data = out.read(chunk) if out else b""
                if not data:
                    self.available = False
                    break
                now = time.monotonic()
                if len(data) % 2:
                    data = data[:-1]
                samples = array.array("h")
                samples.frombytes(data)
                before = det._samples_seen
                found = det.feed(samples)
                if any(abs(s) > 64 for s in samples[::32]):
                    self.last_audio_at = now
                total = before + len(samples)
                for end_idx, strength in found:
                    # sample index → how long ago that moment was, relative to "now"
                    ago = max(total - end_idx, 0) / det.rate
                    with self._lock:
                        self._beats.append((now - ago + self.offset, strength))
        finally:
            try:
                self._proc.terminate()
            except OSError:
                pass


# --------------------------------------------------------------------------
# Painting
# --------------------------------------------------------------------------

_SHADES = ("░", "▒", "▓")


def beat_level(beats: Sequence[Tuple[float, float]], now: float, decay: float = 0.17) -> float:
    """0..1, jumps to the beat's strength and decays exponentially."""
    if not beats:
        return 0.0
    t, strength = beats[-1]
    age = now - t
    if age < 0:
        return 0.0
    return strength * math.exp(-age / decay)


def _shade(level: float) -> Optional[str]:
    if level > 0.62:
        return _SHADES[2]
    if level > 0.36:
        return _SHADES[1]
    if level > 0.14:
        return _SHADES[0]
    return None


def paint_stage_lights(canvas: Canvas, x0: int, y0: int, w: int, h: int, fx: ArtistFx,
                       now: float, beats: Sequence[Tuple[float, float]]) -> None:
    """Moving-head spotlights hanging from the top edge that sweep side to side,
    flare on each beat, plus floor glow and a few sparkles on the hit."""
    if w < 10 or h < 6:
        return
    level = beat_level(beats, now)
    colors = fx.colors
    n = 5 if w >= 90 else 4 if w >= 60 else 3
    dark = (22, 8, 16)

    for i in range(n):
        fx_x = x0 + (i + 0.5) * w / n
        col = colors[i % len(colors)]
        sway = math.sin(now * 0.55 + i * 1.9) * 0.85 + (i - (n - 1) / 2) * 0.28
        for y in range(1, h):
            t = y / h
            cx = fx_x + sway * y * 1.9
            half = 0.7 + y * 0.24 + level * 1.4
            inten = (1.0 - t * 0.72) * (0.32 + 0.68 * level)
            lo, hi = int(cx - half), int(cx + half) + 1
            for x in range(max(lo, x0), min(hi, x0 + w)):
                edge = abs(x - cx) / half
                lv = inten * (1.0 - edge * 0.65)
                ch = _shade(lv)
                if ch is None or canvas.chars[y0 + y][x] != " ":
                    continue
                canvas.put(x, y0 + y, ch, style(lerp(dark, col, min(1.0, lv * 1.6))))
        # the lamp head itself
        head = lerp(lerp(dark, col, 0.55), (255, 255, 255), level * 0.7)
        hx = int(fx_x)
        canvas.put(hx - 1, y0, "▟", style(head))
        canvas.put(hx, y0, "█", style(head, bold=True))
        canvas.put(hx + 1, y0, "▙", style(head))

    # floor wash
    for k in range(2):
        y = y0 + h - 1 - k
        lv = (0.30 + 0.9 * level) * (1.0 - 0.45 * k)
        ch = _shade(lv)
        if ch is None:
            continue
        for x in range(x0, x0 + w):
            if canvas.chars[y][x] == " ":
                col = colors[(x * len(colors)) // max(w, 1) % len(colors)]
                canvas.put(x, y, ch, style(lerp(dark, col, min(1.0, lv * 1.3))))

    # sparkles on the hit (deterministic per beat so they don't shimmer randomly each frame)
    if level > 0.35 and beats:
        rng = random.Random(int(beats[-1][0] * 1000))
        for _ in range(int(6 + 10 * level)):
            sx, sy = rng.randrange(x0, x0 + w), y0 + rng.randrange(1, h)
            if canvas.chars[sy][sx] == " ":
                canvas.put(sx, sy, rng.choice("✦·*+"), style(colors[rng.randrange(len(colors))], bold=True))


def paint_confetti(canvas: Canvas, x0: int, y0: int, w: int, h: int, fx: ArtistFx,
                   now: float, beats: Sequence[Tuple[float, float]]) -> None:
    """Every beat launches a burst of colored paper that tumbles down."""
    if w < 10 or h < 6:
        return
    glyphs = "▪▫◆◇●○■□"
    for t, strength in beats[-4:]:
        age = now - t
        if age < 0 or age > 2.4:
            continue
        rng = random.Random(int(t * 1000))
        for _ in range(int(24 + 40 * strength)):
            px = rng.uniform(x0, x0 + w - 1)
            speed = rng.uniform(4.0, 11.0)
            drift = rng.uniform(-3.0, 3.0)
            col = fx.colors[rng.randrange(len(fx.colors))]
            ch = glyphs[rng.randrange(len(glyphs))]
            y = int(age * speed)
            x = int(px + drift * age + math.sin(age * 5 + px) * 0.8)
            if 0 <= y < h and x0 <= x < x0 + w and canvas.chars[y0 + y][x] == " ":
                fade = 1.0 - age / 2.4
                canvas.put(x, y0 + y, ch, style(lerp((30, 30, 30), col, 0.4 + 0.6 * fade)))


def paint_effect(canvas: Canvas, x0: int, y0: int, w: int, h: int, fx: ArtistFx,
                 now: float, beats: Sequence[Tuple[float, float]]) -> None:
    if fx.effect == EFFECT_CONFETTI:
        paint_confetti(canvas, x0, y0, w, h, fx, now, beats)
    else:
        paint_stage_lights(canvas, x0, y0, w, h, fx, now, beats)
