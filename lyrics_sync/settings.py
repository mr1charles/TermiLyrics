"""Small JSON stores for things that should survive a restart:

  settings.json     the live display settings (mode, theme, animation, ...)
                    so the app opens the way you left it
  adjustments.json  per-song sync fixes (offset, lyrics speed) made with the
                    arrow/+/- keys, re-applied whenever that song plays again

Writes are atomic (temp file + rename); unreadable files are ignored, never
fatal.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError as e:
        log.debug("could not write %s: %s", path, e)


class SettingsStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Dict[str, Any]:
        return _read_json(self.path)

    def save(self, data: Dict[str, Any]) -> None:
        _write_json(self.path, data)


@dataclass
class Adjustment:
    offset: float = 0.0              # seconds, + = lyrics earlier
    scale: Optional[float] = None    # manual lyrics-speed override
    match_length: bool = False       # "v": derive speed from track vs. original length

    @property
    def is_default(self) -> bool:
        return abs(self.offset) < 1e-6 and self.scale is None and not self.match_length


class AdjustmentStore:
    """Per-track sync adjustments. Keyed by song identity plus rounded
    track length, so the original and its slowed upload (or two different
    uploads of one song) each keep their own fix."""

    MAX_ENTRIES = 2000

    def __init__(self, path: Path):
        self.path = path
        self._data: Optional[Dict[str, Any]] = None

    @staticmethod
    def key_for(identity: str, length: Optional[float]) -> str:
        return f"{identity}|{int(round(length)) if length else '?'}"

    def _load(self) -> Dict[str, Any]:
        if self._data is None:
            self._data = _read_json(self.path)
        return self._data

    def get(self, key: str) -> Adjustment:
        raw = self._load().get(key)
        if not isinstance(raw, dict):
            return Adjustment()
        try:
            scale = raw.get("scale")
            return Adjustment(offset=float(raw.get("offset", 0.0)),
                              scale=float(scale) if scale else None,
                              match_length=bool(raw.get("match_length", False)))
        except (TypeError, ValueError):
            return Adjustment()

    def set(self, key: str, adj: Adjustment) -> None:
        data = self._load()
        if adj.is_default:
            data.pop(key, None)
        else:
            data[key] = {"offset": round(adj.offset, 3), "scale": adj.scale, "match_length": adj.match_length}
            while len(data) > self.MAX_ENTRIES:
                data.pop(next(iter(data)))
        _write_json(self.path, data)
