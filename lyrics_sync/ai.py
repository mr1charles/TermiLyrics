"""Optional local-only AI hooks. No cloud APIs are ever called — the app is
fully functional without this module or any of its optional dependencies."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

try:
    from sentence_transformers import SentenceTransformer
    HAVE_EMBEDDINGS = True
except ImportError:
    HAVE_EMBEDDINGS = False


class LocalAlignmentAssist:
    """Optional embedding-based similarity to complement the token/ratio
    scoring in alignment.py (useful for heavily reworded transcript text).
    Loads a small local sentence-transformers model on first use; if the
    package or model isn't available, every method degrades to a no-op —
    callers must treat `None` as "no opinion", not an error."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        self.model_name = model_name
        self._model: Optional["SentenceTransformer"] = None
        self.enabled = HAVE_EMBEDDINGS

    def _ensure_model(self) -> None:
        if self._model is None and self.enabled:
            try:
                self._model = SentenceTransformer(self.model_name)
            except Exception as e:
                log.info("local embedding model unavailable, disabling AI assist: %s", e)
                self.enabled = False

    def similarity(self, a: str, b: str) -> Optional[float]:
        if not self.enabled:
            return None
        self._ensure_model()
        if not self.enabled or self._model is None:
            return None
        try:
            import numpy as np
            vecs = self._model.encode([a, b])
            denom = (np.linalg.norm(vecs[0]) * np.linalg.norm(vecs[1])) or 1.0
            return float(np.dot(vecs[0], vecs[1]) / denom)
        except Exception as e:
            log.debug("embedding similarity failed: %s", e)
            return None


_PROMPT_TEMPLATE = """You clean up messy media-player metadata into a real song artist and title.

Raw title field: {title!r}
Raw artist field: {artist!r}

The artist field may actually be a YouTube uploader/channel name (e.g. a
lyric-video channel, a genre-compilation channel, "Topic" auto-channel)
rather than the real recording artist. Use your knowledge of real songs to
figure out the actual artist and title. If you don't recognize the song and
can't confidently improve on the raw fields, return them unchanged.

Respond with ONLY a JSON object, no other text:
{{"artist": "...", "title": "..."}}"""


class OllamaSongIdentifier:
    """Optional local song-identification assist via a locally-running Ollama
    server (http://localhost:11434) — no cloud calls, no audio capture, just
    an LLM reasoning over the same noisy metadata the regex parser already
    sees. Used only as a fallback when detect.py's parser has low confidence
    in the artist it guessed (see Song.artist_confidence); the fast regex
    path stays the default for everything else.

    Entirely optional: if Ollama isn't running, the configured model isn't
    pulled, or anything about the call fails, `identify()` returns None and
    callers keep using the regex-parsed Song untouched."""

    def __init__(self, model: str = "llama3.2:1b", host: str = "http://localhost:11434",
                 timeout: float = 4.0):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self._availability_checked = False
        self._available = False

    def available(self) -> bool:
        if not self._availability_checked:
            self._availability_checked = True
            try:
                with urllib.request.urlopen(f"{self.host}/api/tags", timeout=2.0) as resp:
                    self._available = resp.status == 200
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                log.info("Ollama not reachable at %s, AI song-ID disabled: %s", self.host, e)
                self._available = False
        return self._available

    def identify(self, raw_title: str, raw_artist: str) -> Optional[tuple[str, str]]:
        """Returns (artist, title) if the model produced a confident-looking
        answer, else None. Safe to call speculatively — never raises."""
        if not self.available():
            return None

        prompt = _PROMPT_TEMPLATE.format(title=raw_title, artist=raw_artist)
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.1},
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self.host}/api/generate", data=payload,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as e:
            log.debug("Ollama song-ID request failed: %s", e)
            return None

        raw_response = body.get("response", "")
        try:
            parsed = json.loads(raw_response)
            artist, title = parsed.get("artist", "").strip(), parsed.get("title", "").strip()
        except (json.JSONDecodeError, AttributeError) as e:
            log.debug("Ollama song-ID returned unparseable JSON: %s", e)
            return None

        if not artist or not title:
            return None
        return artist, title

