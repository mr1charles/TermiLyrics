"""Entry point: wires the modules together and runs the async event loop.

Two concurrent tasks:
  player_watcher()  polls playerctl, detects song changes / pause / seek,
                    feeds the sync clock, triggers lyric loading
  render_loop()     reads keys, asks the sync clock where we are, renders

Song identification, lyric fetch, word-timing upgrades and cache writes
never happen on the render loop's critical path — they run in background
threads (see run_blocking), guarded by a generation counter so a slow
fetch for a song that already changed can never overwrite the new one.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import math
import os
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from . import __version__
from .ai import OllamaSongIdentifier
from .cache import JsonCache
from .config import AppConfig
from .detect import Song, identify, is_advertisement
from .fingerprint import AudioIdentifier
from .fonts import AccentStrippingFont, FontEngine, RasterUnicodeFont, StaticBlockFont
from .keyboard import (BACKSPACE_KEYS, KEY_ESC, KEY_LEFT, KEY_RIGHT, KEY_SHIFT_TAB, TAB_KEY,
                       RawKeyboard)
from .lyrics import LyricsResult, LyricsService
from .player import PlayerSource, PlayerState
from .renderer import (ANIMATIONS, DISPLAY_LABELS, DISPLAY_MODES, MODE_ALIASES, TEXT_STYLES,
                       LiveRenderState, Renderer, Scene, StatusInfo)
from .settings import Adjustment, AdjustmentStore, SettingsStore
from .sync import SyncEngine
from .terminal import TerminalSession, detect_color_mode, set_color_mode
from .themes import THEMES
from .timing import LyricTimeline, TempoEstimate, TempoMap, variant_scale

log = logging.getLogger("lyrics_sync")

SIGNAL_RELOAD = "/tmp/lyrics_force_reload"   # mysong-fix  : non-destructive re-fetch
SIGNAL_RETRY = "/tmp/lyrics_force_retry"     # mysong-retry: evict cache + re-scrape

OFFSET_STEP = 0.1
OFFSET_BIG_STEP = 1.0
SPEED_STEP = 0.01


def _consume_signal(path: str) -> bool:
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
        return True
    return False


async def run_blocking(fn: Callable[..., Any], *args: Any) -> Any:
    """Run a blocking call in a *daemon* thread and await it.

    asyncio.to_thread uses the loop's default executor, whose threads are
    joined at shutdown — so a lyric provider stuck in a retry loop would
    make quitting hang. A daemon thread is simply abandoned at exit."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def deliver(setter: Callable[[Any], None], value: Any) -> None:
        if not fut.done():
            setter(value)

    def worker() -> None:
        try:
            result = fn(*args)
        except BaseException as e:  # noqa: BLE001 — re-raised in the awaiting coroutine
            try:
                loop.call_soon_threadsafe(deliver, fut.set_exception, e)
            except RuntimeError:
                pass  # loop already closed
        else:
            try:
                loop.call_soon_threadsafe(deliver, fut.set_result, result)
            except RuntimeError:
                pass

    threading.Thread(target=worker, daemon=True, name=getattr(fn, "__name__", "worker")).start()
    return await fut


class LyricsApp:
    def __init__(self, config: AppConfig, player: Any = None, lyrics_source: Any = None):
        self.cfg = config
        config.paths.ensure()

        self.player = player or PlayerSource(config.preferred_players)
        self.sync = SyncEngine(config.sync)

        # Optional local model: used when detect.py's parser has LOW
        # confidence in the guessed artist, and as a last-resort lyric
        # search fallback (lyrics.py's _scrape). Silently does nothing if
        # Ollama isn't running.
        self.song_identifier: Optional[OllamaSongIdentifier] = (
            OllamaSongIdentifier(model=config.ollama_model, host=config.ollama_host,
                                 timeout=config.song_id_timeout_seconds)
            if config.ai_enabled and lyrics_source is None else None
        )

        caelestia_dir = Path(os.path.expanduser("~/.cache/caelestia/lyrics/LRCLIB"))
        self.lyrics_service = lyrics_source or LyricsService(
            JsonCache(config.paths.lyrics_cache),
            config.providers,
            caelestia_dir=caelestia_dir if caelestia_dir.exists() else None,
            song_identifier=self.song_identifier,
            lrclib_enabled=config.lrclib_enabled,
            musicbrainz_enabled=config.musicbrainz_enabled,
            user_agent=config.user_agent,
            http_timeout=config.http_timeout_seconds,
        )

        # Bundled packs (repo's fonts/ dir) first, so the user's own packs in
        # ~/Lyrics-Sync/fonts can override any glyph.
        static_font = StaticBlockFont(None, height=config.render.font_height)
        for pack_dir in (Path(__file__).resolve().parent.parent / "fonts", config.paths.fonts_dir):
            if pack_dir.exists():
                static_font._load_packs(pack_dir)
        accent_font = AccentStrippingFont(static_font)
        raster_font = RasterUnicodeFont(cache_dir=config.paths.glyph_cache)
        # Order matters: exact glyph, then an accent-stripped same-width
        # substitute (é->e), and only then the system-font raster fallback.
        self.font_engine = FontEngine([static_font, accent_font, raster_font],
                                      height=config.render.font_height)

        self.settings_store = SettingsStore(config.paths.settings_file)
        self.live = LiveRenderState.from_config(config.render)
        self.renderer = Renderer(self.font_engine, config.render, live=self.live)
        self.adjustments = AdjustmentStore(config.paths.adjustments_file)

        # Last-resort fallback for content with no text clue at all — see
        # fingerprint.py. Off unless the user has configured an API key.
        self.audio_identifier: Optional[AudioIdentifier] = (
            AudioIdentifier(api_key=config.acoustid_api_key,
                            sample_seconds=config.acoustid_sample_seconds,
                            lookup_timeout=config.acoustid_timeout_seconds,
                            tmp_dir=config.paths.home,
                            tempo_factors=config.acoustid_tempo_factors)
            if config.acoustid_api_key and lyrics_source is None else None
        )
        self.identifying_by_audio = False

        self.current_song: Optional[Song] = None
        self.latest_state: Optional[PlayerState] = None
        self.player_missing = True
        self.lyrics = LyricsResult()
        self.timeline: Optional[LyricTimeline] = None
        self.loading = False
        # True once a fetch completed and genuinely found nothing — distinct
        # from "still fetching" and from an instrumental gap between lines.
        self.lyrics_missing = False
        # True while an ad is detected. The sync clock is left alone (not
        # reset), so the real song resumes exactly where it was.
        self.ad_playing = False

        # Length of the recording the lyric timestamps belong to — the
        # reference for re-timing slowed/sped-up tracks.
        self.original_length: Optional[float] = None
        self.tempo_estimate = TempoEstimate(1.0, "none")
        self.adjustment = Adjustment()
        self._adjust_key = ""
        self._last_length: Optional[float] = None

        # Guards the preload race: every song change bumps this, and a
        # background result only commits if it's still the newest.
        self._preload_generation = 0
        self._preload_task: Optional[asyncio.Task] = None
        self._background: List[asyncio.Task] = []

        self._timeline_version = 0   # bumped whenever self.timeline is replaced
        self._word_sync_ok = config.word_sync_enabled

        self._toast = ""
        self._toast_until = 0.0
        self._help_visible = False
        self._help_until = 0.0
        self._retry_requested = False
        self._running = True

    # ------------------------------------------------------------------
    # Tempo / offset
    # ------------------------------------------------------------------

    def tempo_map(self) -> TempoMap:
        scale = self.adjustment.scale or self.tempo_estimate.scale
        return TempoMap(scale=scale, offset=self.cfg.sync.global_offset_seconds + self.adjustment.offset)

    def _scale_source(self) -> str:
        return "manual" if self.adjustment.scale else self.tempo_estimate.source

    def _recompute_tempo(self) -> None:
        song = self.current_song
        if song is None:
            self.tempo_estimate = TempoEstimate(1.0, "none")
            return
        length = self.latest_state.length if self.latest_state else None
        if not self.cfg.auto_tempo_for_variants and not self.adjustment.match_length:
            self.tempo_estimate = TempoEstimate(1.0, "none")
            return
        self.tempo_estimate = variant_scale(song.variant, song.speed_hint, length, self.original_length,
                                            force=self.adjustment.match_length)

    def _save_adjustment(self) -> None:
        if self._adjust_key:
            self.adjustments.set(self._adjust_key, self.adjustment)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    async def _maybe_refine_with_ai(self, song: Song, raw_title: str, raw_artist: str) -> Song:
        """When the regex parser wasn't confident about the artist, ask the
        local Ollama model (if available) to disambiguate uploader-name vs.
        real-artist. Bounded by song_id_timeout_seconds; any failure just
        returns `song` as-is."""
        if not self.song_identifier or song.artist_confidence != "low":
            return song
        try:
            refined = await asyncio.wait_for(
                run_blocking(self.song_identifier.identify, raw_title, raw_artist),
                timeout=self.cfg.song_id_timeout_seconds + 1.0)
        except Exception as e:  # local model calls are best-effort, never fatal
            log.debug("AI song identification failed: %s", e)
            return song
        if refined:
            artist, title = refined
            log.info("AI refined %r/%r -> artist=%r title=%r", raw_artist, raw_title, artist, title)
            return dataclasses.replace(song, artist=artist, title=title, artist_confidence="high")
        return song

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background = [t for t in self._background if not t.done()]
        self._background.append(task)

    def _set_timeline(self, timeline: Optional[LyricTimeline]) -> None:
        self.timeline = timeline
        self._timeline_version += 1

    def _commit_lyrics(self, result: LyricsResult) -> None:
        self.lyrics = result
        self.lyrics_missing = not result.lines
        self._set_timeline(LyricTimeline(result.lines) if result.lines else None)
        if result.duration:
            self.original_length = result.duration
        self.sync.adapt_to_lyrics(self.timeline.min_line_gap() if self.timeline else None)
        self._recompute_tempo()

    async def preload(self, song: Song, generation: int, force_retry: bool = False,
                      allow_audio_fallback: bool = True, player_length: Optional[float] = None) -> None:
        """Off the render loop's critical path: identify -> fetch -> cache.
        Bounded by fetch_timeout_seconds; only commits if `generation` is
        still the newest (see _preload_generation)."""
        self.loading = True
        try:
            result = await asyncio.wait_for(
                run_blocking(self.lyrics_service.fetch, song, force_retry, player_length),
                timeout=self.cfg.fetch_timeout_seconds,
            )
        except asyncio.TimeoutError:
            log.warning("lyric fetch for %r timed out after %.0fs", song, self.cfg.fetch_timeout_seconds)
            result = LyricsResult()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # any provider/parse failure: degrade, don't hang or crash
            log.warning("lyric fetch for %r failed: %s", song, e)
            result = LyricsResult()

        if generation != self._preload_generation:
            log.debug("discarding stale preload result for %r", song)
            return
        self._commit_lyrics(result)
        self.loading = False

        if result.lines:
            if song.variant and not self.original_length:
                self._spawn(self._lookup_original_length(song, generation))
            if self._word_sync_ok and not result.has_word_timing:
                self._spawn(self._upgrade_word_timing(song, generation))
        elif allow_audio_fallback and self.audio_identifier and not result.instrumental:
            self._spawn(self._try_audio_fallback(generation))

    async def _upgrade_word_timing(self, song: Song, generation: int) -> None:
        """Line-synced lyrics are already on screen; try to swap in real
        word-by-word timing for the karaoke modes."""
        base = list(self.lyrics.lines)
        try:
            lines = await asyncio.wait_for(run_blocking(self.lyrics_service.fetch_word_timing, song, base),
                                           timeout=40.0)
        except asyncio.TimeoutError:
            # The provider is hanging (rate-limited/blocked) — stop asking
            # for the rest of this session instead of piling up more.
            log.info("word timing lookup timed out; disabling it for this session")
            self._word_sync_ok = False
            return
        except Exception as e:
            log.debug("word timing upgrade failed: %s", e)
            return
        if generation != self._preload_generation or not lines:
            return
        self.lyrics = dataclasses.replace(self.lyrics, lines=list(lines))
        self._set_timeline(LyricTimeline(lines))
        self.toast("Word-by-word timing loaded")

    async def _lookup_original_length(self, song: Song, generation: int) -> None:
        try:
            duration = await asyncio.wait_for(run_blocking(self.lyrics_service.lookup_duration, song),
                                              timeout=20.0)
        except Exception as e:
            log.debug("duration lookup failed: %s", e)
            return
        if generation != self._preload_generation or not duration:
            return
        self.original_length = duration
        self._recompute_tempo()
        if self.tempo_estimate.source == "durations":
            self.toast(f"Matched {song.variant_label} speed ×{self.tempo_estimate.scale:.2f}")

    async def _try_audio_fallback(self, generation: int) -> None:
        """Text-based identification found nothing — record a sample of
        what's actually playing, fingerprint it (at several speed
        corrections, for slowed/sped-up uploads) and look it up. Fires once
        per song and only acts if the song hasn't changed meanwhile."""
        if generation != self._preload_generation or self.audio_identifier is None:
            return
        variant = self.current_song.variant if self.current_song else ""
        self.identifying_by_audio = True
        try:
            match = await run_blocking(self.audio_identifier.identify, variant)
        except Exception as e:  # best-effort, never fatal
            log.debug("audio fingerprint identification failed: %s", e)
            match = None
        finally:
            self.identifying_by_audio = False

        if generation != self._preload_generation or match is None:
            return
        log.info("audio fingerprint identified: %r (speed %.2f)", match.song, match.speed)
        self.current_song = match.song
        if match.duration:
            self.original_length = match.duration
        self._recompute_tempo()
        length = self.latest_state.length if self.latest_state else None
        await self.preload(match.song, generation, allow_audio_fallback=False, player_length=length)

    async def _start_song(self, song: Song, state: PlayerState, force_retry: bool) -> None:
        self.current_song = song
        self.sync.reset(song.identity, state)
        self._preload_generation += 1
        generation = self._preload_generation
        if self._preload_task and not self._preload_task.done():
            self._preload_task.cancel()
        self.lyrics = LyricsResult()  # don't keep showing the previous song's lyrics
        self._set_timeline(None)
        self.lyrics_missing = False
        self.original_length = None
        self._last_length = state.length
        self._adjust_key = AdjustmentStore.key_for(song.identity, state.length)
        self.adjustment = self.adjustments.get(self._adjust_key)
        self._recompute_tempo()

        coro = self.preload(song, generation, force_retry=force_retry, player_length=state.length)
        if self.cfg.preload_before_playback:
            await coro
        else:
            self._preload_task = asyncio.create_task(coro)

    async def player_watcher(self) -> None:
        last_identity: Optional[str] = None
        while self._running:
            reload_requested = _consume_signal(SIGNAL_RELOAD)
            retry_requested = _consume_signal(SIGNAL_RETRY) or self._retry_requested
            self._retry_requested = False

            try:
                state = await asyncio.to_thread(self.player.read)
            except Exception as e:  # a broken player backend must never kill the app
                log.debug("player read failed: %s", e)
                state = None
            self.latest_state = state

            if state is None:
                self.player_missing = True
                self.current_song = None
                last_identity = None
                self.ad_playing = False
                await asyncio.sleep(1.0)
                continue
            self.player_missing = False

            if is_advertisement(state.title, state.artist):
                # Don't touch the song or sync at all — when the ad ends,
                # the real song resumes exactly where we left it.
                self.ad_playing = True
                await asyncio.sleep(self.sync.poll_interval)
                continue
            self.ad_playing = False

            detected = identify(state.title, state.artist, album=state.album)
            if detected.identity != last_identity or reload_requested or retry_requested:
                last_identity = detected.identity
                song = await self._maybe_refine_with_ai(detected, state.title, state.artist)
                await self._start_song(song, state, force_retry=retry_requested)
            else:
                self.sync.observe(state)
                if state.length != self._last_length:
                    self._last_length = state.length
                    self._recompute_tempo()

            await asyncio.sleep(self.sync.poll_interval)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def toast(self, text: str, seconds: float = 1.8) -> None:
        self._toast = text
        self._toast_until = time.monotonic() + seconds

    def _save_settings(self) -> None:
        self.settings_store.save(self.live.to_dict())

    def _adjust_offset(self, delta: float) -> None:
        self.adjustment.offset = round(self.adjustment.offset + delta, 3)
        self._save_adjustment()
        off = self.adjustment.offset
        if abs(off) < 1e-6:
            self.toast("Lyrics offset: none")
        else:
            self.toast(f"Lyrics {abs(off):.1f}s {'earlier' if off > 0 else 'later'}")

    def _adjust_speed(self, delta: float) -> None:
        # Scaled around 0:00, not around "now": a slowed/sped-up edit is a
        # uniform resample of the whole track, so drift grows linearly from
        # the start — once the speed is right, it's right everywhere.
        current = self.adjustment.scale or self.tempo_estimate.scale
        new = round(min(2.0, max(0.5, current + delta)), 3)
        self.adjustment.scale = None if abs(new - self.tempo_estimate.scale) < 1e-6 else new
        self._save_adjustment()
        self.toast(f"Lyrics speed ×{new:.2f}")

    def _handle_key(self, key: str) -> None:
        live = self.live
        if key in ("q", "Q"):
            self._running = False
            return
        if key in ("h", "?"):
            self._help_visible = not self._help_visible
            self._help_until = time.monotonic() + 15.0
            return
        if key == KEY_ESC:
            self._help_visible = False
            return

        changed = True
        if key in ("tab", TAB_KEY, "m"):
            live.cycle_display_mode()
            self.toast(DISPLAY_LABELS.get(live.display_mode, live.display_mode))
        elif key == KEY_SHIFT_TAB or key == "M":
            live.cycle_display_mode(-1)
            self.toast(DISPLAY_LABELS.get(live.display_mode, live.display_mode))
        elif len(key) == 1 and key in "123456":
            live.set_display_mode(DISPLAY_MODES[int(key) - 1])
            self.toast(DISPLAY_LABELS.get(live.display_mode, live.display_mode))
        elif key in BACKSPACE_KEYS or key == "c":
            live.cycle_theme()
            self.toast(f"Theme: {THEMES[live.theme].name}")
        elif key == "C":
            live.cycle_theme(-1)
            self.toast(f"Theme: {THEMES[live.theme].name}")
        elif key == "e":
            live.cycle_animation()
            self.toast(f"Animation: {live.animation}")
        elif key == "t":
            live.toggle_typing_effect()
            self.toast(f"Typing effect: {'on' if live.typing_effect else 'off'}")
        elif key == "a":
            live.cycle_text_style()
            self.toast(f"Text size: {live.text_style}")
        elif key == "r":
            live.toggle_romanize()
            self.toast(f"Romanize: {'on' if live.romanize else 'off'}")
        elif key == "p":
            live.toggle_status_bar()
        else:
            changed = False
        if changed:
            self._save_settings()
            return

        if key in (KEY_LEFT, ","):
            self._adjust_offset(+OFFSET_STEP)
        elif key in (KEY_RIGHT, "."):
            self._adjust_offset(-OFFSET_STEP)
        elif key == "<":
            self._adjust_offset(+OFFSET_BIG_STEP)
        elif key == ">":
            self._adjust_offset(-OFFSET_BIG_STEP)
        elif key in ("-", "_"):
            self._adjust_speed(-SPEED_STEP)
        elif key in ("+", "="):
            self._adjust_speed(+SPEED_STEP)
        elif key == "v":
            self.adjustment.match_length = not self.adjustment.match_length
            self.adjustment.scale = None
            self._recompute_tempo()
            self._save_adjustment()
            if self.adjustment.match_length and self.tempo_estimate.source in ("durations", "exact"):
                self.toast(f"Matched to track length: ×{self.tempo_estimate.scale:.2f}")
            elif self.adjustment.match_length:
                self.toast("Track/original length unknown — use - / + instead")
            else:
                self.toast("Match speed to track length: off")
        elif key == "0":
            self.adjustment = Adjustment()
            self._recompute_tempo()
            self._save_adjustment()
            self.toast("Sync reset for this song")
        elif key == "f":
            self._retry_requested = True
            self.toast("Searching for lyrics again…")
        elif key in ("space", "n", "b"):
            action = {"space": "play-pause", "n": "next", "b": "previous"}[key]
            player = self.latest_state.player if self.latest_state else None
            try:
                self.player.command(player, action)
            except Exception as e:
                log.debug("player command failed: %s", e)

    def _help_lines(self) -> List[Tuple[str, str]]:
        adj = self.adjustment
        tm = self.tempo_map()
        return self.renderer.default_help_lines(extra=[
            ("", ""),
            ("← / →  (, .)", f"lyrics earlier / later     offset {adj.offset:+.1f}s"),
            ("< / >", "same, in 1-second steps"),
            ("- / +", f"lyrics slower / faster     ×{tm.scale:.2f} ({self._scale_source()})"),
            ("v", f"match speed to track length   {'on' if adj.match_length else 'off'}"),
            ("0", "reset sync for this song"),
            ("f", "search for lyrics again"),
            ("space  n  b", "play/pause · next · previous"),
            ("h / ?", "close this help            q  quit"),
        ])

    def _status(self, position: float) -> Optional[StatusInfo]:
        song = self.current_song
        if song is None:
            return None
        st = self.latest_state
        tm = self.tempo_map()
        return StatusInfo(title=song.title, artist=song.artist, position=position,
                          length=st.length if st else None, playing=self.sync.playing,
                          scale=tm.scale, scale_source=self._scale_source(), variant=song.variant,
                          offset=tm.offset, word_sync=self.lyrics.has_word_timing,
                          player_rate=self.sync.rate, source=self.lyrics.source)

    def build_scene(self, now: float) -> Scene:
        toast = self._toast if now < self._toast_until else ""
        if self._help_visible and now > self._help_until:
            self._help_visible = False
        help_lines = self._help_lines() if self._help_visible else []
        base = dict(now=now, toast=toast, show_help=self._help_visible, help_lines=help_lines)
        song = self.current_song
        title = song.title if song else ""

        if self.player_missing:
            if shutil.which("playerctl") is None and isinstance(self.player, PlayerSource):
                detail = ("playerctl isn't installed — install it with your package manager\n"
                          "(e.g. sudo pacman -S playerctl  ·  sudo apt install playerctl)\n"
                          "or try the demo:  termilyrics --demo")
            else:
                detail = ("Play something in Spotify, a browser (YouTube), mpv, VLC…\n"
                          "anything playerctl can see.   Or try:  termilyrics --demo")
            return Scene(message="No music player detected", message_detail=detail, **base)
        if self.ad_playing:
            return Scene(message="Advertisement — lyrics paused", **base)

        snap = self.sync.snapshot(now)
        status = self._status(snap.position)
        if self.identifying_by_audio:
            return Scene(message="Listening to identify the song…", spinner=True, status=status, **base)
        if self.timeline is None:
            if self.loading or song is None:
                return Scene(message=f"Finding lyrics for {title or '…'}", spinner=True,
                             message_detail=song.artist if song else "", status=status, **base)
            if self.lyrics.instrumental:
                return Scene(message="♪  Instrumental  ♪", message_detail=title, status=status, **base)
            return Scene(message=f"No synced lyrics found for {title}",
                         message_detail="f: search again   ·   h: help", status=status, **base)

        tm = self.tempo_map()
        cursor = self.timeline.locate(tm.to_lyric(snap.position))
        return Scene(cursor=cursor, timeline=self.timeline, scale=tm.scale * self.sync.rate,
                     status=status, **base)

    def _signature(self, scene: Scene, cols: int, rows: int) -> tuple:
        c = scene.cursor
        st = scene.status
        return (cols, rows, self.live.version, scene.message, scene.message_detail, scene.toast,
                scene.show_help, tuple(scene.help_lines), c.index, c.active, c.word_index,
                self._timeline_version,
                (int(st.position), st.playing, round(st.scale, 3), round(st.offset, 2), st.word_sync)
                if st and self.live.status_bar else None)

    async def render_loop(self, terminal: TerminalSession, keyboard: RawKeyboard) -> None:
        last_sig: Optional[tuple] = None
        animating = False
        while self._running:
            size = terminal.size()
            while True:
                key = keyboard.poll()
                if key is None:
                    break
                self._handle_key(key)
            if not self._running:
                break

            now = time.monotonic()
            scene = self.build_scene(now)
            sig = self._signature(scene, size.cols, size.rows)
            if animating or sig != last_sig:
                frame = self.renderer.render(scene, size.cols, size.rows)
                terminal.draw(frame.lines, size)
                last_sig = sig
                # While paused nothing should move — don't burn CPU on it.
                animating = frame.animating and (self.sync.playing or bool(scene.message))
            fps = max(5, min(self.cfg.render.fps, 120))
            await asyncio.sleep(1.0 / fps if animating else self.cfg.sync.clock_tick_seconds)

    async def run(self) -> None:
        with TerminalSession() as terminal, RawKeyboard() as keyboard:
            watcher = asyncio.create_task(self.player_watcher())
            try:
                await self.render_loop(terminal, keyboard)
            finally:
                self._running = False
                watcher.cancel()
                for t in self._background:
                    t.cancel()


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------

def _mode_arg(value: str) -> str:
    v = value.strip().lower().replace("-", "_")
    if v in DISPLAY_MODES:
        return v
    alias = MODE_ALIASES.get(value.strip().lower()) or MODE_ALIASES.get(v)
    if alias:
        return alias
    raise argparse.ArgumentTypeError(f"unknown mode {value!r} (see --list)")


def _choice(options, name: str):
    def parse(value: str) -> str:
        v = value.strip().lower().replace("-", "_")
        if v in options:
            return v
        raise argparse.ArgumentTypeError(f"unknown {name} {value!r} — choose from: {', '.join(options)}")
    return parse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="termilyrics",
        description="Synced, animated lyrics in your terminal for whatever is playing "
                    "(Spotify, YouTube in a browser, mpv, VLC, … via playerctl).",
        epilog="Keys while running: Tab/1-6 mode · c theme · e animation · a text size · "
               "←/→ lyrics earlier/later · -/+ lyrics speed · v match slowed/sped-up length · "
               "f re-search · space play/pause · h help · q quit",
    )
    p.add_argument("legacy_mode", nargs="?", metavar="1|2",
                   help="legacy shortcut: 1 = normal, 2 = typing effect")
    p.add_argument("-m", "--mode", type=_mode_arg,
                   help="display mode: minimalist, karaoke (sing-along), scroll, word_pop, box, matrix (or 1-6)")
    p.add_argument("-t", "--theme", type=_choice(tuple(THEMES), "theme"), help="color theme (see --list)")
    p.add_argument("-e", "--animation", "--effect", type=_choice(ANIMATIONS, "animation"),
                   help="line-change animation: " + ", ".join(ANIMATIONS))
    p.add_argument("-s", "--style", type=_choice(TEXT_STYLES, "text size"),
                   help="text size: auto, block (giant), medium, plain")
    p.add_argument("--typing", action="store_true", help="typewriter effect (same as --animation typewriter)")
    p.add_argument("--offset", type=float, metavar="SECONDS",
                   help="show all lyrics this much earlier (negative = later), e.g. 0.2 for Bluetooth audio")
    p.add_argument("--fps", type=int, help="animation frame rate (default 30)")
    p.add_argument("--no-status", action="store_true", help="hide the bottom status bar")
    p.add_argument("--no-romanize", action="store_true", help="show non-Latin lyrics in their own script")
    p.add_argument("--player", action="append", metavar="NAME",
                   help="prefer this player (e.g. spotify, firefox, mpv); repeatable")
    p.add_argument("--demo", nargs="?", const="", metavar="LRC_FILE",
                   help="play a built-in demo song (or your own .lrc file) without a music player")
    p.add_argument("--speed", type=float, default=1.0,
                   help="with --demo: pretend the track is slowed/sped up by this factor (e.g. 0.8)")
    p.add_argument("--no-word-sync", action="store_true", help="don't fetch word-by-word timing")
    p.add_argument("--no-lrclib", action="store_true", help="skip the direct LrcLib lookup")
    p.add_argument("--no-ai", action="store_true", help="never query a local Ollama model")
    p.add_argument("--acoustid-key", metavar="KEY", help="enable audio-fingerprint song recognition")
    p.add_argument("--color", choices=("auto", "truecolor", "256", "16", "none"), default="auto",
                   help="terminal color support (default: detect)")
    p.add_argument("--reset-settings", action="store_true", help="ignore the display settings saved last time")
    p.add_argument("--list", action="store_true", help="list display modes, themes and animations, then exit")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging to ~/Lyrics-Sync/termilyrics.log")
    p.add_argument("--version", action="version", version=f"TermiLyrics {__version__}")
    return p


def _print_list() -> None:
    print("Display modes (Tab or 1-6 while running):")
    for n, m in enumerate(DISPLAY_MODES, 1):
        print(f"  {n}  {m:<16} {DISPLAY_LABELS[m]}")
    print("\nThemes (c / Backspace):")
    for k, t in THEMES.items():
        print(f"     {k:<22} {t.name}")
    print("\nAnimations (e):")
    print("     " + ", ".join(ANIMATIONS))
    print("\nText sizes (a):")
    print("     " + ", ".join(TEXT_STYLES))


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list:
        _print_list()
        return

    config = AppConfig()
    render = config.render
    if args.legacy_mode is not None:
        legacy = args.legacy_mode.strip().lower()
        if legacy in ("2", "typing"):
            render = dataclasses.replace(render, typing_effect=True)
        elif legacy not in ("1", "normal"):
            parser.error(f"unknown mode {args.legacy_mode!r} — use 1 (normal) or 2 (typing), or --mode")
    if args.typing:
        render = dataclasses.replace(render, typing_effect=True)
    if args.fps:
        render = dataclasses.replace(render, fps=args.fps)
    sync = config.sync
    if args.offset is not None:
        sync = dataclasses.replace(sync, global_offset_seconds=args.offset)
    config = dataclasses.replace(
        config, render=render, sync=sync,
        word_sync_enabled=config.word_sync_enabled and not args.no_word_sync,
        lrclib_enabled=config.lrclib_enabled and not args.no_lrclib,
        ai_enabled=config.ai_enabled and not args.no_ai,
        acoustid_api_key=args.acoustid_key or config.acoustid_api_key,
        preferred_players=tuple(args.player) if args.player else config.preferred_players,
    )

    config.paths.ensure()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        filename=str(config.paths.log_file),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    set_color_mode(detect_color_mode() if args.color == "auto" else args.color)

    player = lyrics_source = None
    if args.demo is not None:
        from .demo import load_demo
        try:
            player, lyrics_source = load_demo(args.demo or None, args.speed)
        except OSError as e:
            parser.error(f"can't read {args.demo!r}: {e}")

    app = LyricsApp(config, player=player, lyrics_source=lyrics_source)
    if not args.reset_settings:
        app.live.apply_dict(app.settings_store.load())
    overrides = {}
    if args.mode:
        overrides["display_mode"] = args.mode
    if args.theme:
        overrides["theme"] = args.theme
    if args.animation:
        overrides["animation"] = args.animation
    if args.typing or (args.legacy_mode or "").strip().lower() in ("2", "typing"):
        overrides["animation"] = "typewriter"
    if args.style:
        overrides["text_style"] = args.style
    if args.no_status:
        overrides["status_bar"] = False
    if args.no_romanize:
        overrides["romanize"] = False
    if overrides:
        app.live.apply_dict(overrides)

    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
