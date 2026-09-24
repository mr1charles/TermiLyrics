import re

import pytest

from lyrics_sync.canvas import Canvas, text_width
from lyrics_sync.config import RenderConfig
from lyrics_sync.demo import DEMO_LRC
from lyrics_sync.fonts import AccentStrippingFont, FontEngine, StaticBlockFont
from lyrics_sync.lyrics import parse_lrc, parse_lrc_document
from lyrics_sync.renderer import (ANIMATIONS, DISPLAY_KARAOKE, DISPLAY_MODES, TEXT_STYLES, LiveRenderState,
                                  Renderer, Scene, StatusInfo)
from lyrics_sync.themes import THEMES
from lyrics_sync.timing import LyricTimeline

ANSI = re.compile(r"\033\[[0-9;]*m")
TL = LyricTimeline(parse_lrc_document(DEMO_LRC).lines)


def renderer(**live):
    cfg = RenderConfig()
    font = StaticBlockFont()
    r = Renderer(FontEngine([font, AccentStrippingFont(font)]), cfg)
    for k, v in live.items():
        setattr(r.live, k, v)
    return r


def scene(t, tl=TL):
    return Scene(now=t, cursor=tl.locate(t), timeline=tl,
                 status=StatusInfo(title="Twinkle", artist="Jane", position=t, length=70.0))


def visible_width(line):
    return text_width(ANSI.sub("", line))


@pytest.mark.parametrize("mode", DISPLAY_MODES)
@pytest.mark.parametrize("size", [(10, 4), (40, 10), (80, 24), (200, 60)])
def test_every_mode_fits_every_size(mode, size):
    cols, rows = size
    r = renderer(display_mode=mode)
    for t in (0.0, 2.5, 5.0, 6.3, 20.0, 37.0, 70.0):
        frame = r.render(scene(t), cols, rows)
        assert len(frame.lines) == rows
        assert all(visible_width(l) <= cols for l in frame.lines)


@pytest.mark.parametrize("animation", ANIMATIONS)
@pytest.mark.parametrize("style", TEXT_STYLES)
def test_every_animation_and_text_style_renders(animation, style):
    r = renderer(animation=animation, text_style=style)
    for t in (4.8, 4.85, 4.95, 5.2, 8.0):
        frame = r.render(scene(t), 120, 30)
        assert any(ANSI.sub("", l).strip() for l in frame.lines)


def test_transition_reports_animating_then_settles():
    r = renderer(animation="fade", display_mode="minimalist")
    assert r.render(scene(4.85), 120, 30).animating
    assert not r.render(scene(8.0), 120, 30).animating


def test_every_theme_renders():
    for key in THEMES:
        r = renderer(theme=key, display_mode=DISPLAY_KARAOKE)
        assert r.render(scene(6.0), 100, 30).lines


def test_karaoke_ball_sits_over_the_word_being_sung():
    r = renderer(display_mode=DISPLAY_KARAOKE, text_style="plain")
    tl = LyricTimeline(parse_lrc("[00:01.00]<00:01.00>left <00:02.00>middle <00:03.00>right<00:04.00>"))
    frame = r.render(Scene(now=0, cursor=tl.locate(1.0), timeline=tl), 60, 20)
    plain = [ANSI.sub("", l) for l in frame.lines]
    text_row = next(i for i, l in enumerate(plain) if "left middle right" in l)
    ball_row = next(i for i, l in enumerate(plain) if "●" in l)
    assert ball_row < text_row
    word_start = plain[text_row].index("left")
    assert word_start <= plain[ball_row].index("●") <= word_start + 4


def test_karaoke_lights_sung_words():
    r = renderer(display_mode=DISPLAY_KARAOKE, text_style="plain", theme="classic_mono")
    tl = LyricTimeline(parse_lrc("[00:01.00]<00:01.00>aaa <00:02.00>bbb<00:03.00>"))
    frame = r.render(Scene(now=0, cursor=tl.locate(2.99), timeline=tl), 40, 12)
    line = next(l for l in frame.lines if "aaa" in ANSI.sub("", l))
    lit = THEMES["classic_mono"].highlight
    assert f"38;2;{lit[0]};{lit[1]};{lit[2]}" in line.split("aaa")[0][-40:]


def test_message_scene_and_help_overlay():
    r = renderer()
    f = r.render(Scene(now=1.0, message="No music player detected", message_detail="try --demo"), 80, 24)
    text = "\n".join(ANSI.sub("", l) for l in f.lines)
    assert "No music player detected" in text and "try --demo" in text
    f = r.render(Scene(now=1.0, show_help=True), 80, 24)
    assert "TermiLyrics — keys" in "\n".join(ANSI.sub("", l) for l in f.lines)


def test_wide_characters_never_overflow():
    r = renderer(display_mode=DISPLAY_KARAOKE, romanize=False)
    tl = LyricTimeline(parse_lrc("[00:01.00]" + "你好世界" * 12))
    frame = r.render(Scene(now=0, cursor=tl.locate(1.5), timeline=tl), 30, 12)
    assert all(visible_width(l) <= 30 for l in frame.lines)


def test_legacy_build_frame_api():
    r = renderer()
    frame = r.build_frame("hello world", 80, 24)
    assert any("█" in ANSI.sub("", l) for l in frame.lines)
    assert r.build_settings_overlay(80, 24).lines


def test_canvas_wide_char_overwrite_is_repaired():
    c = Canvas(6, 1)
    c.text(0, 0, "你好")
    c.put(1, 0, "x")  # clobbers the right half of 你
    assert c.plain_lines()[0] == " x好"  # the orphaned half of 你 became a space


def test_live_state_round_trip():
    live = LiveRenderState.from_config(RenderConfig())
    live.cycle_display_mode()
    live.cycle_theme()
    other = LiveRenderState.from_config(RenderConfig())
    other.apply_dict(live.to_dict())
    assert other.to_dict() == live.to_dict()
    other.apply_dict({"theme": "nope", "display_mode": 42})
    assert other.theme == live.theme


def test_new_song_never_reuses_previous_songs_layout():
    r = renderer(display_mode="scroll")
    for n in range(30):  # timelines get garbage-collected; ids can be reused
        tl = LyricTimeline(parse_lrc(f"[00:01.00]song number {n}"))
        text = "\n".join(ANSI.sub("", l) for l in r.render(Scene(now=0, cursor=tl.locate(1.5), timeline=tl), 60, 12).lines)
        assert f"song number {n}" in text
        del tl


def test_explicit_block_size_is_honoured_for_native_scripts():
    from lyrics_sync.fonts import SIZE_BIG
    from lyrics_sync.renderer import PLAIN_MODE
    r = renderer(romanize=False, text_style="block")
    text, force_plain = r._prepare_text("你好")
    assert force_plain
    assert r._choose_size([(text, True)], 100, 30, force_plain)[0] == SIZE_BIG
    r.live.text_style = "auto"
    assert r._choose_size([(text, True)], 100, 30, force_plain)[0] == PLAIN_MODE
