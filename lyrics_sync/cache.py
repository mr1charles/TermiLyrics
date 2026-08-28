"""Disk cache for lyrics/transcripts: atomic writes, corruption-safe reads."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional


def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name.strip()[:180] or "untitled"


class TextCache:
    """key -> text file cache. Writes are atomic; corrupted reads self-evict
    instead of crashing or serving garbage forever."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str, ext: str) -> Path:
        return self.directory / f"{safe_filename(key)}.{ext}"

    def read(self, key: str, ext: str = "lrc") -> Optional[str]:
        path = self._path(key, ext)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
            return text if text.strip() else None
        except (OSError, UnicodeDecodeError):
            self.evict(key, ext)
            return None

    def write(self, key: str, text: str, ext: str = "lrc") -> None:
        if not text:
            return
        path = self._path(key, ext)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.directory), prefix=".tmp_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_path, path)  # atomic on POSIX
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    def evict(self, key: str, ext: str = "lrc") -> None:
        try:
            self._path(key, ext).unlink(missing_ok=True)
        except OSError:
            pass


class JsonCache(TextCache):
    def read_json(self, key: str, ext: str = "json") -> Any:
        raw = self.read(key, ext)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self.evict(key, ext)
            return None

    def write_json(self, key: str, obj: Any, ext: str = "json") -> None:
        try:
            self.write(key, json.dumps(obj, ensure_ascii=False), ext)
        except (TypeError, ValueError):
            pass
