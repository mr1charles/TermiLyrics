import math

from lyrics_sync.lyrics import parse_lrc
from lyrics_sync.timing import (DEFAULT_SLOWED_SCALE, LyricTimeline, TempoMap, estimate_syllables,
                                estimate_words, split_words, variant_scale)


def test_syllables():
    assert estimate_syllables("a") == 1
    assert estimate_syllables("beautiful") >= 3
    assert estimate_syllables("你好") == 2


def test_split_words_cjk():
    assert split_words("你好 hello") == [("你", False), ("好", True), ("hello", True)]


def test_estimated_words_are_ordered_and_bounded():
    words = estimate_words("How I wonder what you are", 10.0, 14.0)
    assert [w.text for w in words] == ["How", "I", "wonder", "what", "you", "are"]
    assert words[0].start == 10.0
    for a, b in zip(words, words[1:]):
        assert a.start < b.start and abs(a.end - b.start) < 1e-9
    assert words[-1].end <= 14.0
    # "wonder" (2 syllables) takes longer than "I" (1)
    assert (words[2].end - words[2].start) > (words[1].end - words[1].start)


def test_long_gap_does_not_stretch_the_line():
    words = estimate_words("short line", 10.0, 40.0)
    assert words[-1].end < 14.0  # sung in a few seconds, not across the whole 30s gap


def test_locate_walks_intro_line_words_gap_and_outro():
    tl = LyricTimeline(parse_lrc("[00:05.00]<00:05.00>one <00:06.00>two <00:07.00>three<00:08.00>\n"
                                 "[00:30.00]last line"))
    intro = tl.locate(2.0)
    assert not intro.active and intro.next_index == 0 and 0.35 < intro.gap_progress < 0.45
    c = tl.locate(6.5)
    assert c.active and c.index == 0 and c.word_index == 1 and abs(c.word_progress - 0.5) < 1e-6
    gap = tl.locate(20.0)
    assert not gap.active and gap.next_index == 1 and 0 < gap.gap_progress < 1
    outro = tl.locate(100.0)
    assert not outro.active and outro.next_index == -1 and math.isinf(outro.gap_length)


def test_explicit_empty_line_ends_the_previous_one():
    tl = LyricTimeline(parse_lrc("[00:01.00]a\n[00:02.00]\n[00:09.00]b"))
    assert tl.locate(1.5).active
    assert not tl.locate(3.0).active


def test_tempo_map_roundtrip():
    tm = TempoMap(scale=0.8, offset=0.3)
    assert tm.to_lyric(100.0) == 80.3
    assert abs(tm.to_player(tm.to_lyric(42.0)) - 42.0) < 1e-9


def test_variant_scale_prefers_durations_then_title_then_guess():
    assert variant_scale("slowed", None, 250.0, 200.0).scale == 0.8
    assert variant_scale("slowed", None, 250.0, 200.0).source == "durations"
    assert variant_scale("slowed", None, 200.0, 201.0).source == "exact"
    assert variant_scale("slowed", 0.85, None, None).scale == 0.85
    guess = variant_scale("slowed", None, None, None)
    assert guess.scale == DEFAULT_SLOWED_SCALE and guess.is_guess
    assert variant_scale("sped_up", None, 160.0, 200.0).scale == 1.25


def test_variant_scale_ignores_contradicting_durations():
    # "slowed" but the track is *shorter* than the original: durations are
    # measuring something else (a cut), fall back to the hint/guess.
    assert variant_scale("slowed", None, 150.0, 200.0).source == "guess"


def test_untagged_tracks_are_never_stretched_unless_forced():
    assert variant_scale("", None, 250.0, 200.0).scale == 1.0  # e.g. music video with an intro
    assert variant_scale("", None, 250.0, 200.0, force=True).scale == 0.8
