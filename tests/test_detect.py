import pytest

from lyrics_sync.detect import detect_variant, identify, strip_noise_tags


@pytest.mark.parametrize("title,expected", [
    ("Blinding Lights (Slowed + Reverb)", ("slowed", None)),
    ("Heat Waves - sped up", ("sped_up", None)),
    ("Song (Nightcore)", ("sped_up", None)),
    ("Glimpse of Us (super slowed)", ("slowed", None)),
    ("Song 0.8x", ("slowed", 0.8)),
    ("Die For You x1.25", ("sped_up", 1.25)),
    ("Song - Slowed Down + Reverb 85%", ("slowed", 0.85)),
    ("Song (reverb)", ("", None)),          # reverb alone doesn't change tempo
    ("Song (8D Audio)", ("", None)),
    ("100% Pure Love", ("", None)),
])
def test_detect_variant(title, expected):
    assert detect_variant(title) == expected


@pytest.mark.parametrize("raw,clean", [
    ("Blinding Lights (Slowed + Reverb)", "Blinding Lights"),
    ("Song (slowed 80%)", "Song"),
    ("Song [slowed + reverb]", "Song"),
    ("100% Pure Love", "100% Pure Love"),
    ("Bad Romance (Official Music Video)", "Bad Romance"),
])
def test_noise_stripping(raw, clean):
    assert strip_noise_tags(raw) == clean


def test_slowed_version_shares_cache_key_but_not_identity():
    original = identify("Blinding Lights", "The Weeknd")
    slowed = identify("Blinding Lights (Slowed + Reverb)", "The Weeknd")
    assert slowed.variant == "slowed"
    assert slowed.key == original.key
    assert slowed.identity != original.identity


def test_existing_title_parsing_still_works():
    s = identify("Lady Gaga - Bad Romance (Official Music Video)", "Pizza Music")
    assert (s.artist, s.title, s.artist_confidence) == ("Lady Gaga", "Bad Romance", "low")
    s = identify("Cuéntame", "Enjambre - Topic")
    assert (s.artist, s.title) == ("Enjambre", "Cuéntame")
    s = identify("7clouds - Artist - Song (Lyrics) - Mozilla Firefox", "")
    assert (s.artist, s.title) == ("Artist", "Song")


def test_remix_suffix_is_not_the_title():
    s = identify("Trndsttr (feat. M. Maggie) - Lucian Remix", "Black Coast", "spotify")
    assert (s.artist, s.title) == ("Black Coast", "Trndsttr")


def test_remaster_suffix_is_not_the_title():
    s = identify("Hello - Remastered 2011", "Adele", "spotify")
    assert (s.artist, s.title) == ("Adele", "Hello")


def test_super_slowed_suffix_keeps_real_title():
    s = identify("VANITY FUNK - SUPER SLOWED", "NTRXBRST", "spotify")
    assert (s.artist, s.title) == ("NTRXBRST", "VANITY FUNK")


def test_spotify_ad_detected_by_url():
    from lyrics_sync.detect import is_advertisement
    assert is_advertisement("LISTEN NOW", "", "https://open.spotify.com/ad/c37fef509707421984bca5807fe40561")
    assert is_advertisement("Listen to music, ad-free.", "", "https://open.spotify.com/ad/abc")
    assert not is_advertisement("Mandem Style", "Luke Day", "https://open.spotify.com/track/5hKQHXGzM8pxLeestSXu32")
    assert not is_advertisement("LISTEN NOW", "Some Band", "")


def test_arabic_and_hebrew_are_left_in_native_script():
    from lyrics_sync.languages import romanize
    for text in ("يلحقها من بيت لبيت ويقلها اعطيني بوسة", "שלום עולם"):
        r = romanize(text)
        assert not r.was_romanized and r.text == text
