import json
import math
import random

import pytest

from lyrics_sync.canvas import Canvas
from lyrics_sync.config import RenderConfig
from lyrics_sync.demo import DEMO_LRC
from lyrics_sync.effects import (ARTIST_FX, BeatDetector, EFFECT_CONFETTI, EFFECT_STAGE_LIGHTS, beat_level,
                                 fx_for_artist, load_user_fx, paint_effect)
from lyrics_sync.fonts import AccentStrippingFont, FontEngine, StaticBlockFont
from lyrics_sync.lyrics import parse_lrc_document
from lyrics_sync.renderer import DISPLAY_MODES, LiveRenderState, Renderer, Scene, StatusInfo
from lyrics_sync.timing import LyricTimeline

RATE = 11025


def drum_track(bpm, seconds, hats=True):
    random.seed(7)
    n = int(RATE * seconds)
    s = [0.0] * n
    period = int(RATE * 60 / bpm)
    for k in range(0, n, period):                         # kick drum thump
        for i in range(min(int(RATE * 0.18), n - k)):
            s[k + i] += 0.8 * math.exp(-i / (RATE * 0.05)) * math.sin(2 * math.pi * 55 * i / RATE)
    if hats:
        for k in range(period // 2, n, period // 2):     # off-beat hi-hats
            for i in range(min(int(RATE * 0.03), n - k)):
                s[k + i] += 0.25 * (random.random() * 2 - 1)
    for i in range(n):                                    # sustained bass line
        s[i] += 0.12 * math.sin(2 * math.pi * 80 * i / RATE)
    return [int(max(-1, min(1, v)) * 32767) for v in s]


@pytest.mark.parametrize("bpm", [100, 128, 150])
def test_detector_finds_the_kicks(bpm):
    beats = BeatDetector().feed(drum_track(bpm, 20))
    expected = int(20 * bpm / 60)
    assert expected - 3 <= len(beats) <= expected + 1
    times = [i / RATE for i, _ in beats]
    gaps = sorted(b - a for a, b in zip(times, times[1:]))
    assert gaps[len(gaps) // 2] == pytest.approx(60 / bpm, abs=0.03)


def test_detector_ignores_silence_and_never_flashes_faster_than_the_gap():
    assert BeatDetector().feed([0] * RATE * 5) == []
    beats = BeatDetector(min_gap=0.28).feed(drum_track(150, 15))
    times = [i / RATE for i, _ in beats]
    assert all(b - a >= 0.28 - 1e-9 for a, b in zip(times, times[1:]))


def test_detector_works_on_arbitrary_chunk_sizes():
    samples = drum_track(120, 10)
    whole = BeatDetector().feed(samples)
    d = BeatDetector()
    chunked = []
    for i in range(0, len(samples), 777):
        chunked.extend(d.feed(samples[i:i + 777]))
    assert [b[0] for b in chunked] == [b[0] for b in whole]


@pytest.mark.parametrize("artist,name", [
    ("BLACKPINK", "BLACKPINK"), ("Jennie", "BLACKPINK"), ("ROSÉ", "BLACKPINK"),
    ("Selena Gomez, BLACKPINK", "BLACKPINK"), ("BLACKPINK & Selena Gomez", "BLACKPINK"),
    ("(G)I-DLE", "(G)I-DLE"), ("Stray Kids", "Stray Kids"), ("NCT WISH", "NCT WISH"),
])
def test_artist_matching(artist, name):
    assert fx_for_artist(artist).name == name


def test_unknown_artists_get_nothing():
    assert fx_for_artist("Halsey") is None
    assert fx_for_artist("") is None


def test_user_file_adds_and_overrides_artists(tmp_path):
    f = tmp_path / "artist_fx.json"
    f.write_text(json.dumps({"Halsey": {"effect": "confetti", "colors": ["#ff00ff"]},
                             "BLACKPINK": {"effect": "confetti", "colors": ["#00ff00"]}}))
    extra = load_user_fx(f)
    assert fx_for_artist("Halsey", extra).effect == EFFECT_CONFETTI
    assert fx_for_artist("BLACKPINK", extra).colors == ((0, 255, 0),)
    f.write_text("{not json")
    assert load_user_fx(f) == {}
    assert load_user_fx(tmp_path / "missing.json") == {}


def test_beat_level_jumps_then_decays():
    beats = ((10.0, 1.0),)
    assert beat_level(beats, 9.9) == 0.0
    assert beat_level(beats, 10.0) == pytest.approx(1.0)
    assert beat_level(beats, 10.5) < beat_level(beats, 10.1) < 1.0
    assert beat_level((), 10.0) == 0.0


@pytest.mark.parametrize("name", ["BLACKPINK", "Coldplay"])
def test_effects_paint_only_blank_cells_and_stay_in_bounds(name):
    fx = fx_for_artist(name)
    canvas = Canvas(100, 30)
    canvas.put(50, 15, "X")
    paint_effect(canvas, 0, 0, 100, 30, fx, 10.0, ((9.95, 1.0),))
    assert canvas.chars[15][50] == "X"
    assert any(c != " " for row in canvas.chars for c in row)


def test_tiny_terminals_do_not_crash():
    for w, h in ((3, 3), (10, 5), (0, 0)):
        paint_effect(Canvas(w, h), 0, 0, w, h, fx_for_artist("BLACKPINK"), 1.0, ((0.9, 1.0),))


def _renderer(**live):
    font = StaticBlockFont()
    r = Renderer(FontEngine([font, AccentStrippingFont(font)]), RenderConfig())
    for k, v in live.items():
        setattr(r.live, k, v)
    return r


TL = LyricTimeline(parse_lrc_document(DEMO_LRC).lines)


def _scene(t, fx, beats):
    return Scene(now=t, cursor=TL.locate(t), timeline=TL, fx=fx, beats=beats,
                 status=StatusInfo(title="T", artist="A", position=t, length=70.0))


@pytest.mark.parametrize("mode", DISPLAY_MODES)
def test_every_mode_renders_with_an_effect_and_keeps_animating(mode):
    r = _renderer(display_mode=mode)
    frame = r.render(_scene(6.0, fx_for_artist("BLACKPINK"), ((5.95, 1.0),)), 100, 30)
    assert frame.lines
    from lyrics_sync.renderer import DISPLAY_HACKER_MATRIX
    assert frame.animating or mode == DISPLAY_HACKER_MATRIX


def test_effects_toggle_removes_them():
    scene = _scene(6.0, fx_for_artist("BLACKPINK"), ((5.95, 1.0),))
    on = _renderer(effects=True).render(scene, 100, 30)
    off = _renderer(effects=False).render(scene, 100, 30)
    assert on.animating and not off.animating
    assert on.lines != off.lines


def test_effect_uses_artist_palette_not_the_theme():
    r = _renderer()
    r.render(_scene(6.0, fx_for_artist("BLACKPINK"), ()), 100, 30)
    assert r._theme.primary == ARTIST_FX["blackpink"].colors[0]
    r.render(_scene(6.0, None, ()), 100, 30)
    assert r._theme.name != "BLACKPINK"


def test_effects_setting_round_trips():
    live = LiveRenderState.from_config(RenderConfig())
    live.toggle_effects()
    data = live.to_dict()
    assert data["effects"] is False
    other = LiveRenderState.from_config(RenderConfig())
    other.apply_dict(data)
    assert other.effects is False
