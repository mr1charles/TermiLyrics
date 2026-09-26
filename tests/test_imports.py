import importlib
import pkgutil

import lyrics_sync


def test_every_module_imports():
    for mod in pkgutil.iter_modules(lyrics_sync.__path__):
        if mod.name != "__main__":
            importlib.import_module(f"lyrics_sync.{mod.name}")


def test_alignment_still_resolves_repeated_choruses_forward():
    from lyrics_sync.alignment import ForwardAligner
    from lyrics_sync.config import AlignmentConfig
    from lyrics_sync.lyrics import LyricLine
    from lyrics_sync.transcript import TranscriptLine
    lyrics = [LyricLine(10.0, "we will rock you"), LyricLine(20.0, "we will rock you")]
    transcript = [TranscriptLine(11.0, 2.0, "we will rock you"), TranscriptLine(31.0, 2.0, "we will rock you")]
    aligned = ForwardAligner(AlignmentConfig()).align(lyrics, transcript)
    assert [a.timestamp for a in aligned] == [11.0, 31.0]
