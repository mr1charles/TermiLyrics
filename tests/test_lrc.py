from lyrics_sync.lyrics import current_and_next, parse_lrc, parse_lrc_document


def test_multiple_timestamps_repeat_the_line():
    lines = parse_lrc("[00:10.00][01:30.00]Chorus line\n[00:20.00]Verse")
    assert [(l.timestamp, l.text) for l in lines] == [(10.0, "Chorus line"), (20.0, "Verse"), (90.0, "Chorus line")]


def test_offset_tag_shifts_everything_earlier():
    lines = parse_lrc("[offset:+500]\n[00:10.00]a\n[00:20.00]b")
    assert [l.timestamp for l in lines] == [9.5, 19.5]


def test_time_formats():
    lines = parse_lrc("[00:05]a\n[00:06.5]b\n[00:07:25]c\n[01:02.123]d")
    assert [round(l.timestamp, 3) for l in lines] == [5.0, 6.5, 7.25, 62.123]


def test_metadata_and_length():
    doc = parse_lrc_document("[ti:Song]\n[ar:Band]\n[length: 03:25]\n[00:01.00]x")
    assert (doc.title, doc.artist, doc.length) == ("Song", "Band", 205.0)


def test_credit_and_spam_lines_are_dropped():
    lines = parse_lrc("[00:00.00] 作词 : Somebody\n[00:00.50]Written by: X\n"
                      "[00:01.00]www.RentAnAdviser.com\n[00:02.00]Real lyric")
    assert [l.text for l in lines] == ["Real lyric"]


def test_empty_lines_are_kept_as_gap_markers():
    lines = parse_lrc("[00:01.00]a\n[00:05.00]\n[00:09.00]b")
    assert [l.text for l in lines] == ["a", "", "b"]


def test_enhanced_word_timing_with_end_marker():
    (line,) = parse_lrc("[00:12.00]<00:12.00>Hello <00:12.50>big <00:12.90>world<00:13.50>")
    assert line.text == "Hello big world"
    assert [(w.text, w.start) for w in line.words] == [("Hello", 12.0), ("big", 12.5), ("world", 12.9)]
    assert line.words[-1].end == 13.5


def test_richsync_format_from_syncedlyrics():
    (line,) = parse_lrc("[00:15.00] <00:15.00> Rich <00:15.40>   <00:15.50> sync <00:16.00>   <00:16.10> line ")
    assert line.text == "Rich sync line"
    assert [(w.text, w.start, w.end) for w in line.words][:2] == [("Rich", 15.0, 15.4), ("sync", 15.5, 16.0)]


def test_cjk_words_split_per_character_without_spaces():
    (line,) = parse_lrc("[00:18.00]<00:18.00>你<00:18.30>好<00:18.60>世界")
    assert line.text == "你好世界"
    assert [w.text for w in line.words] == ["你", "好", "世", "界"]
    assert not any(w.space_after for w in line.words[:-1])
    starts = [w.start for w in line.words]
    assert starts == sorted(starts) and len(set(starts)) == 4


def test_word_tags_respect_offset():
    (line,) = parse_lrc("[offset:1000]\n[00:10.00]<00:10.00>a <00:10.50>b")
    assert line.timestamp == 9.0
    assert [w.start for w in line.words] == [9.0, 9.5]


def test_current_and_next_compat():
    lines = parse_lrc("[00:01.00]a\n[00:05.00]b")
    assert current_and_next(lines, 0.5) == ("", "a")
    assert current_and_next(lines, 2.0) == ("a", "b")
    assert current_and_next(lines, 30.0, max_hold_seconds=8.0) == ("", "")
