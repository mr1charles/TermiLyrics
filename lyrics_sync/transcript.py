"""Best-effort YouTube transcript retrieval, used only to repair or fill in
missing/mistimed LRC lyrics via alignment.py. Entirely optional — absence
of a transcript (or the dependency) never blocks playback."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

log = logging.getLogger(__name__)

try:
    from youtube_transcript_api import YouTubeTranscriptApi
    HAVE_YT_TRANSCRIPT = True
except ImportError:
    HAVE_YT_TRANSCRIPT = False


@dataclass(frozen=True)
class TranscriptLine:
    start: float
    duration: float
    text: str


def fetch_transcript(video_id: str) -> List[TranscriptLine]:
    if not HAVE_YT_TRANSCRIPT or not video_id:
        return []
    try:
        raw = YouTubeTranscriptApi.get_transcript(video_id)
    except Exception as e:
        log.debug("transcript fetch failed for %s: %s", video_id, e)
        return []
    return [
        TranscriptLine(start=float(r["start"]), duration=float(r.get("duration", 0.0)), text=r["text"])
        for r in raw
    ]
