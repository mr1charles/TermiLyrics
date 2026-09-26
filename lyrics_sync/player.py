"""Player integration: a defensive wrapper around the playerctl CLI.

Never used for continuous timing (see sync.SyncEngine) — only to establish
ground-truth checkpoints and detect play/pause/seek/song-change events.

Each read is ONE `playerctl --all-players metadata --format ...` process
covering every player at once (the old approach spawned five processes per
player per poll), and every state carries the monotonic time its position
was sampled at, so the sync engine never mistakes "how long ago we asked"
for "how far the song has moved".
"""
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence

_SEP = "<~|~>"
_FIELDS = ("playerInstance", "playerName", "status", "position", "mpris:length", "artist", "title", "album", "xesam:url")
_FORMAT = _SEP.join("{{%s}}" % f for f in _FIELDS)


@dataclass(frozen=True)
class PlayerState:
    player: str
    title: str
    artist: str
    position: float
    status: str  # "Playing" | "Paused" | "Stopped"
    length: Optional[float] = None
    album: str = ""
    url: str = ""
    # time.monotonic() at which `position` was true. 0.0 = unknown (the
    # sync engine then assumes "now").
    sampled_at: float = 0.0

    @property
    def playing(self) -> bool:
        return self.status == "Playing"


class PlayerctlError(RuntimeError):
    pass


def _micros(raw: str) -> Optional[float]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw) / 1_000_000
    except ValueError:
        return None


def parse_all_players(output: str, sampled_at: float) -> List[PlayerState]:
    states: List[PlayerState] = []
    for line in output.splitlines():
        parts = line.split(_SEP)
        if len(parts) != len(_FIELDS):
            continue
        instance, pname, status, pos, length, artist, title, album, url = (p.strip() for p in parts)
        name = instance or pname
        if not title:
            continue
        states.append(PlayerState(
            player=name or "default", title=title, artist=artist,
            position=_micros(pos) or 0.0, status=status or "Playing",
            length=_micros(length) or None, album=album, url=url, sampled_at=sampled_at,
        ))
    return states


class PlayerSource:
    def __init__(self, preferred: Sequence[str] = ("spotify",)):
        self.preferred = tuple(p.lower() for p in preferred)
        self._last_player: Optional[str] = None
        self._bulk_supported = True

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

    def _read_bulk(self) -> Optional[List[PlayerState]]:
        """All players in one process. None if this playerctl can't do it."""
        t0 = time.monotonic()
        try:
            proc = subprocess.run(["playerctl", "--all-players", "metadata", "--format", _FORMAT],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=1.5)
        except FileNotFoundError:
            return []
        except subprocess.TimeoutExpired:
            return None
        t1 = time.monotonic()
        states = parse_all_players(proc.stdout.decode(errors="replace"), (t0 + t1) / 2)
        if not states and proc.returncode not in (0, 1):
            return None
        return states

    def _read_from(self, player: Optional[str]) -> Optional[PlayerState]:
        """Per-field fallback for playerctl builds without --all-players."""
        try:
            title = self._run("metadata", "--format", "{{title}}", player=player)
            if not title:
                return None
            artist = self._run("metadata", "--format", "{{artist}}", player=player)
            status = self._run("status", player=player) or "Playing"
            t0 = time.monotonic()
            pos = float(self._run("position", player=player) or 0.0)
            sampled_at = (t0 + time.monotonic()) / 2
            length_raw = self._run("metadata", "--format", "{{mpris:length}}", player=player)
            length = (float(length_raw) / 1_000_000) if length_raw.isdigit() else None
            return PlayerState(player=player or "default", title=title, artist=artist, position=pos,
                               status=status, length=length, sampled_at=sampled_at)
        except (PlayerctlError, ValueError):
            return None

    def _rank(self, state: PlayerState) -> tuple:
        name = state.player.lower()
        preferred = any(name == p or name.startswith(p + ".") for p in self.preferred)
        return (
            0 if state.playing else 1,                      # playing beats paused
            0 if state.player == self._last_player else 1,  # don't flip between two playing players
            0 if preferred else 1,
        )

    def read(self) -> Optional[PlayerState]:
        """Best available PlayerState across every known player.

        An actively-playing player always wins over a paused/stopped one —
        otherwise a paused background app (Spotify sitting idle, a muted
        browser tab) can shadow the player you're actually listening to.
        Among equals, the player we were already following wins, then the
        preferred ones."""
        states: Optional[List[PlayerState]] = None
        if self._bulk_supported:
            states = self._read_bulk()
            if states is None:
                self._bulk_supported = False
        if states is None:
            states = []
            names: List[Optional[str]] = list(self.preferred)
            for p in self.list_players():
                if p not in names:
                    names.append(p)
            for name in names or [None]:
                s = self._read_from(name)
                if s is not None:
                    states.append(s)
        if not states:
            return None
        best = min(states, key=self._rank)
        self._last_player = best.player
        return best

    def command(self, player: Optional[str], action: str) -> bool:
        """Transport control: 'play-pause', 'next', 'previous'."""
        if action not in ("play-pause", "next", "previous", "play", "pause"):
            return False
        try:
            self._run(action, player=player if player and player != "default" else None)
            return True
        except PlayerctlError:
            return False
