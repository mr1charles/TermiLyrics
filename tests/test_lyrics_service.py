import json

import pytest

from lyrics_sync import lyrics as lyrics_mod
from lyrics_sync.cache import JsonCache
from lyrics_sync.detect import identify
from lyrics_sync.lyrics import LyricsService, lyrics_match, parse_lrc, score_candidates

LRC_A = "[00:10.00]First line here\n[00:15.00]Second line here\n[00:20.00]Third line here\n[00:25.00]Fourth"


def cand(id, track, artist, duration, synced=LRC_A, instrumental=False):
    return {"id": id, "trackName": track, "artistName": artist, "albumName": "", "duration": duration,
            "instrumental": instrumental, "syncedLyrics": synced, "plainLyrics": ""}


class FakeHttp:
    def __init__(self, lrclib=None, musicbrainz=None):
        self.lrclib = lrclib or []
        self.musicbrainz = musicbrainz
        self.calls = []

    def __call__(self, url, params):
        self.calls.append((url, dict(params)))
        if "lrclib" in url:
            return self.lrclib
        if "musicbrainz" in url:
            return self.musicbrainz
        raise AssertionError(url)


@pytest.fixture(autouse=True)
def no_syncedlyrics(monkeypatch):
    monkeypatch.setattr(lyrics_mod, "HAVE_SYNCEDLYRICS", False)


def service(tmp_path, http, **kw):
    return LyricsService(JsonCache(tmp_path), ["LrcLib", "NetEase"], http_get=http, **kw)


def test_duration_picks_the_right_version(tmp_path):
    http = FakeHttp([
        cand(1, "Song", "Artist", 300.0, synced="[00:01.00]extended mix"),
        cand(2, "Song", "Artist", 201.0, synced="[00:01.00]album version"),
    ])
    song = identify("Song", "Artist")
    result = service(tmp_path, http).fetch(song, player_length=200.0)
    assert result.lines[0].text == "album version"
    assert result.duration == 201.0 and result.source == "lrclib"


def test_version_words_are_penalised(tmp_path):
    ranked = score_candidates([cand(1, "Song (Live)", "Artist", 200.0), cand(2, "Song", "Artist", 200.0)],
                              identify("Song", "Artist"), 200.0)
    assert ranked[0].candidate["id"] == 2


def test_wrong_song_is_rejected(tmp_path):
    http = FakeHttp([cand(1, "Completely Different", "Someone Else", 200.0)])
    result = service(tmp_path, http).fetch(identify("Song", "Artist"), player_length=200.0)
    assert not result.lines


def test_slowed_track_uses_original_lyrics_and_reports_their_duration(tmp_path):
    http = FakeHttp([cand(1, "Song", "Artist", 200.0)])
    song = identify("Song (Slowed + Reverb)", "Artist")
    result = service(tmp_path, http).fetch(song, player_length=250.0)
    assert result.lines and result.duration == 200.0


def test_exact_slowed_lrc_beats_retiming_the_original(tmp_path):
    http = FakeHttp([
        cand(1, "Song", "Artist", 200.0, synced="[00:01.00]original"),
        cand(2, "Song (Slowed)", "Artist", 250.0, synced="[00:01.00]made for the slowed upload"),
    ])
    song = identify("Song (Slowed)", "Artist")
    result = service(tmp_path, http).fetch(song, player_length=250.5)
    assert result.lines[0].text == "made for the slowed upload"


def test_instrumental(tmp_path):
    http = FakeHttp([cand(1, "Song", "Artist", 200.0, synced="", instrumental=True)])
    result = service(tmp_path, http).fetch(identify("Song", "Artist"), player_length=200.0)
    assert result.instrumental and not result.lines


def test_cache_round_trip_skips_the_network(tmp_path):
    http = FakeHttp([cand(1, "Song", "Artist", 200.0)])
    svc = service(tmp_path, http)
    song = identify("Song", "Artist")
    svc.fetch(song, player_length=200.0)
    n = len(http.calls)
    again = svc.fetch(song, player_length=200.0)
    assert len(http.calls) == n
    assert again.duration == 200.0 and again.source.startswith("cache")


def test_missing_song_is_negative_cached_until_forced(tmp_path):
    http = FakeHttp([])
    svc = service(tmp_path, http)
    song = identify("Nothing", "Nobody")
    assert not svc.fetch(song).lines
    n = len(http.calls)
    svc.fetch(song)
    assert len(http.calls) == n
    svc.fetch(song, force_retry=True)
    assert len(http.calls) > n


def test_network_failure_is_not_negative_cached(tmp_path):
    def broken(url, params):
        raise OSError("offline")
    svc = service(tmp_path, broken)
    song = identify("Song", "Artist")
    assert not svc.fetch(song).lines
    assert "missing" not in svc._read_meta(song.key)  # retried next time, not remembered as missing


def test_syncedlyrics_fallback_rejects_lyrics_longer_than_the_track(tmp_path, monkeypatch):
    long_lrc = "[00:10.00]a\n[05:00.00]way past the end"
    good_lrc = "[00:10.00]a\n[02:00.00]b"
    calls = []

    class FakeSL:
        @staticmethod
        def search(q, **kw):
            calls.append((q, kw))
            return long_lrc if len(calls) == 1 else good_lrc
    monkeypatch.setattr(lyrics_mod, "HAVE_SYNCEDLYRICS", True)
    monkeypatch.setattr(lyrics_mod, "syncedlyrics", FakeSL, raising=False)
    svc = service(tmp_path, FakeHttp([]))
    result = svc.fetch(identify("Song (Official Video)", "Artist"), player_length=180.0)
    assert [l.text for l in result.lines] == ["a", "b"]
    assert all(kw.get("synced_only") for _, kw in calls)
    assert all("LrcLib" not in kw["providers"] for _, kw in calls)  # already queried directly


def test_word_timing_is_only_accepted_when_it_matches(tmp_path, monkeypatch):
    base = parse_lrc(LRC_A)
    matching = "\n".join(f"[00:{10 + 5 * i:02d}.20]<00:{10 + 5 * i:02d}.20>{t.split()[0]} <00:{10 + 5 * i:02d}.60>line <00:{10 + 5 * i:02d}.90>here"
                         for i, t in enumerate(["First", "Second", "Third"]))
    monkeypatch.setattr(lyrics_mod, "HAVE_SYNCEDLYRICS", True)

    class FakeSL:
        result = matching

        @staticmethod
        def search(q, **kw):
            assert kw.get("enhanced") is True
            return FakeSL.result
    monkeypatch.setattr(lyrics_mod, "syncedlyrics", FakeSL, raising=False)
    svc = service(tmp_path, FakeHttp([]))
    song = identify("Song", "Artist")
    words = svc.fetch_word_timing(song, base)
    assert words and words[0].words
    # cached for next time
    FakeSL.result = ""
    assert svc.fetch_word_timing(song, base)

    other = identify("Other", "Artist")
    FakeSL.result = "[00:40.00]<00:40.00>totally <00:41.00>different <00:42.00>song"
    assert svc.fetch_word_timing(other, base) is None


def test_lyrics_match_needs_similar_text_and_timing():
    a = parse_lrc(LRC_A)
    shifted = parse_lrc(LRC_A.replace("[00:1", "[00:4").replace("[00:2", "[00:5"))
    assert lyrics_match(a, a)
    assert not lyrics_match(a, shifted)


def test_lookup_duration_falls_back_to_musicbrainz(tmp_path):
    http = FakeHttp([], {"recordings": [{"title": "Song", "score": 100, "length": 212000}]})
    svc = service(tmp_path, http)
    assert svc.lookup_duration(identify("Song", "Artist")) == 212.0


def test_only_one_word_timing_lookup_in_flight(tmp_path, monkeypatch):
    monkeypatch.setattr(lyrics_mod, "HAVE_SYNCEDLYRICS", True)
    svc = service(tmp_path, FakeHttp([]))
    svc._words_lock.acquire()  # simulate a lookup stuck in syncedlyrics' retry loop

    class Boom:
        @staticmethod
        def search(q, **kw):
            raise AssertionError("must not start a second lookup")
    monkeypatch.setattr(lyrics_mod, "syncedlyrics", Boom, raising=False)
    assert svc.fetch_word_timing(identify("Song", "Artist"), parse_lrc(LRC_A)) is None
    svc._words_lock.release()


def _lrc(first):
    return f"[{int(first // 60):02d}:{first % 60:05.2f}]Line one\n[00:30.00]Line two"


def test_consensus_prefers_the_timing_most_uploads_agree_on(tmp_path):
    # Two mislabelled uploads carry the original's long intro; four agree on
    # the remix's timing. All fit the track length equally well.
    http = FakeHttp(lrclib=[
        cand(1, "Trndsttr", "Black Coast", 180, _lrc(25.89)),
        cand(2, "Trndsttr", "Black Coast", 180, _lrc(25.89)),
        cand(3, "Trndsttr", "Black Coast", 179, _lrc(0.53)),
        cand(4, "Trndsttr", "Black Coast", 180, _lrc(0.56)),
        cand(5, "Trndsttr", "Black Coast", 180, _lrc(0.58)),
        cand(6, "Trndsttr", "Black Coast", 179, _lrc(0.58)),
    ])
    song = identify("Trndsttr (feat. M. Maggie) - Lucian Remix", "Black Coast", "spotify")
    assert song.version == "Lucian Remix"
    res = service(tmp_path, http).fetch(song, player_length=179.7)
    assert res.lines[0].timestamp < 1.0


def test_far_off_duration_is_penalised():
    song = identify("Song", "Artist", "spotify")
    ranked = score_candidates([cand(1, "Song", "Artist", 244), cand(2, "Song", "Artist", 180)], song, 180)
    assert ranked[0].candidate["id"] == 2
    assert ranked[0].score - ranked[1].score > 0.3


def test_remix_tag_in_the_playing_title_is_not_penalised():
    song = identify("Trndsttr - Lucian Remix", "Black Coast", "spotify")
    ranked = score_candidates([cand(1, "Trndsttr (Lucian Remix)", "Black Coast", 180)], song, 180)
    assert ranked[0].score > 0.95


def test_lrclib_http_get_retries_transient_errors(monkeypatch):
    import io
    import urllib.error
    calls = {"n": 0}

    class Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)
        return Resp(b"[]")

    monkeypatch.setattr(lyrics_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(lyrics_mod.time, "sleep", lambda s: None)
    get = lyrics_mod._default_http_get("ua", 5)
    assert get("https://lrclib.net/api/search", {"q": "x"}) == []
    assert calls["n"] == 3


def test_fallback_result_after_lrclib_failure_is_retried_later(tmp_path, monkeypatch):
    monkeypatch.setattr(lyrics_mod, "HAVE_SYNCEDLYRICS", True)
    monkeypatch.setattr(lyrics_mod.syncedlyrics, "search", lambda *a, **k: LRC_A, raising=False)

    class Down:
        def __call__(self, url, params):
            raise OSError("lrclib unreachable")

    svc = service(tmp_path, Down())
    song = identify("Song", "Artist", "spotify")
    first = svc.fetch(song, player_length=30)
    assert first.source == "syncedlyrics"
    # Fresh cache entry from a failed LrcLib lookup is only a stopgap: once the
    # retry window passes it must not be served as the final answer.
    meta = svc._read_meta(song.key)
    assert meta.get("lrclib_pending") is True
    svc._write_meta(song.key, fetched=0)
    assert svc._from_cache(song.key, svc._read_meta(song.key)) is None
