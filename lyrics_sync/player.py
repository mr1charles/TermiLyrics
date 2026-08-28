"""Player integration: a defensive wrapper around the playerctl CLI.

Never used for continuous timing (see sync.SyncEngine) — only to establish
ground-truth checkpoints and detect play/pause/seek/song-change events.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import List, Optional, Sequence


@dataclass(frozen=True)
class PlayerState:
    player: str
    title: str
    artist: str
    position: float
    status: str  # "Playing" | "Paused" | "Stopped"
    length: Optional[float] = None

    @property
    def playing(self) -> bool:
        return self.status == "Playing"


class PlayerctlError(RuntimeError):
    pass


class PlayerSource:
    def __init__(self, preferred: Sequence[str] = ("spotify",)):
        self.preferred = tuple(preferred)

    def _run(self, *args: str, player: Optional[str] = None) -> str:
        cmd = ["playerctl"]
        if player:
            cmd += ["-p", player]
        cmd += list(args)
        try:
            out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=1.5)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise PlayerctlError(str(e)) from e
        return out.decode(errors="replace").strip()

    def list_players(self) -> List[str]:
        try:
            out = subprocess.check_output(["playerctl", "-l"], stderr=subprocess.DEVNULL, timeout=1.5)
            return [p for p in out.decode(errors="replace").strip().splitlines() if p]
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return []

    def _read_from(self, player: Optional[str]) -> Optional[PlayerState]:
        """Read full metadata for one specific player, or None if it has
        nothing loaded / errors out."""
        try:
            title = self._run("metadata", "--format", "{{title}}", player=player)
            if not title:
                return None
            artist = self._run("metadata", "--format", "{{artist}}", player=player)
            status = self._run("status", player=player) or "Playing"
            pos = float(self._run("position", player=player) or 0.0)
            length_raw = self._run("metadata", "--format", "{{mpris:length}}", player=player)
            length = (float(length_raw) / 1_000_000) if length_raw.isdigit() else None
            return PlayerState(
                player=player or "default",
                title=title,
                artist=artist,
                position=pos,
                status=status,
                length=length,
            )
        except (PlayerctlError, ValueError):
            return None

    def read(self) -> Optional[PlayerState]:
        """Best available PlayerState across every known player.

        An actively-playing player always wins over a paused/stopped one —
        otherwise a paused background app (Spotify sitting idle, a muted
        browser tab) can shadow the player you're actually listening to,
        since both simply "have metadata". Only falls back to whatever has
        metadata at all if nothing is currently playing anywhere.
        """
        candidates: List[Optional[str]] = list(self.preferred)
        for p in self.list_players():
            if p not in candidates:
                candidates.append(p)
        candidates.append(None)  # unnamed default player

        fallback: Optional[PlayerState] = None
        for player in candidates:
            state = self._read_from(player)
            if state is None:
                continue
            if state.playing:
                return state
            if fallback is None:
                fallback = state
        return fallback
