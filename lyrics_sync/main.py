"""Entry point: wires the modules together and runs the async event loop.

Two concurrent tasks:
  player_watcher()  detects song changes / pause / seek, triggers preload
  render_loop()     ticks the local sync clock and redraws the frame

Song identification, lyric fetch, and cache writes never happen on the
render loop's critical path (see LyricsApp.preload) — Problem 5.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import sys
import time
from pathlib import Path
from typing import List, Optional

from .cache import TextCache
from .config import AppConfig
from .ai import OllamaSongIdentifier
from .detect import Song, identify, is_advertisement
from .fingerprint import AudioIdentifier
from .fonts import AccentStrippingFont, FontEngine, RasterUnicodeFont, StaticBlockFont
from .keyboard import RawKeyboard, BACKSPACE_KEYS, TAB_KEY
from .lyrics import LyricLine, LyricsService, current_and_next
from .player import PlayerSource
from .renderer import LiveRenderState, Renderer
from .sync import SyncEngine
from .terminal import TerminalSession

log = logging.getLogger("lyrics_sync")

SIGNAL_RELOAD = "/tmp/lyrics_force_reload"   # mysong-fix  : non-destructive re-fetch
SIGNAL_RETRY = "/tmp/lyrics_force_retry"     # mysong-retry: evict cache + re-scrape


def _consume_signal(path: str) -> bool:
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass
        return True
    return False


class LyricsApp:
    def __init__(self, config: AppConfig):
        self.cfg = config
        config.paths.ensure()

        self.player = PlayerSource()
        self.sync = SyncEngine(config.sync)

        # Optional: primarily invoked when detect.py's regex parser has LOW
        # confidence in the guessed artist (e.g. an uploader/channel name
        # mistaken for the real artist) — see below. Also passed into
        # LyricsService as a genuine last-resort lyric-search fallback (see
        # lyrics.py's _scrape) for tracks a cleaned title still can't
        # match. Availability is probed lazily and cached — if Ollama
        # isn't running, both uses silently do nothing.
        self.song_identifier: Optional[OllamaSongIdentifier] = (
            OllamaSongIdentifier(model=config.ollama_model, host=config.ollama_host,
                                  timeout=config.song_id_timeout_seconds)
            if config.ai_enabled else None
        )

        caelestia_dir = Path(os.path.expanduser("~/.cache/caelestia/lyrics/LRCLIB"))
        self.lyrics_service = LyricsService(
            TextCache(config.paths.lyrics_cache),
            config.providers,
            caelestia_dir=caelestia_dir if caelestia_dir.exists() else None,
            song_identifier=self.song_identifier,
        )

        static_font = StaticBlockFont(config.paths.fonts_dir, height=config.render.font_height)
        accent_font = AccentStrippingFont(static_font)
        raster_font = RasterUnicodeFont(cache_dir=config.paths.glyph_cache)
        # Order matters: exact hand-authored/pack glyph first, then a
        # same-width accent-stripped substitute (é->e) for anything a
        # dedicated font pack didn't cover, and only THEN the fragile
        # system-font raster fallback as a last resort — see fonts.py's
        # AccentStrippingFont docstring for why this ordering is what
        # actually fixes misaligned/broken-looking non-English lyrics.
        self.font_engine = FontEngine(
            [static_font, accent_font, raster_font], height=config.render.font_height
        )
        # Mutable, in-app-adjustable (theme/display-mode/romanize/typing-
        # effect) — see renderer.LiveRenderState. Seeded from config, then
        # changed live via keybinds in render_loop(), never reconstructed.
        self.live = LiveRenderState.from_config(config.render)
        self.renderer = Renderer(self.font_engine, config.render, live=self.live)

        # Last-resort fallback for content with no text clue at all — see
        # fingerprint.py. Off unless the user has configured an API key.
        self.audio_identifier: Optional[AudioIdentifier] = (
            AudioIdentifier(api_key=config.acoustid_api_key,
                             sample_seconds=config.acoustid_sample_seconds,
                             lookup_timeout=config.acoustid_timeout_seconds,
                             tmp_dir=config.paths.home)
            if config.acoustid_api_key else None
        )
        self.identifying_by_audio = False

        self.current_song: Optional[Song] = None
        self.lyric_lines: List[LyricLine] = []
        self.loading = False
        # True once a fetch completed and genuinely found nothing — distinct
        # from `not lyric_lines and not loading` at song start, and distinct
        # from the brief "..." shown for a song's instrumental intro before
        # its first lyric timestamp. Without this, "still fetching",
        # "gave up, no lyrics exist" and "between lyric lines" all render
        # identically, which is especially confusing on non-music content
        # (podcasts, spoken-word videos, ads) that will never have lyrics.
        self.lyrics_missing = False

        # How long a lyric line stays displayed with no next line to bound
        # it (mainly: the outro after the last line). Computed per-song from
        # its own line spacing once lyrics load — see preload(). Default
        # covers the period before any lyrics have loaded yet.
        self.max_lyric_hold_seconds = 8.0

        # Guards the preload race: every song-change bumps this, and a
        # completed preload only commits its result if it's still the
        # newest one in flight. Without this, a slow fetch for a song that
        # already changed again can finish *after* a newer, faster fetch
        # and silently overwrite good lyrics with stale/empty ones —
        # freezing the display on "..." with no further trigger to recover,
        # since nothing else will re-run preload until the next song change.
        self._preload_generation = 0
        self._preload_task: Optional[asyncio.Task] = None

        # True while an ad is detected as playing. The sync clock is frozen
        # (not ticked at all) during an ad rather than reset — most ad
        # breaks don't even change the player's reported metadata, and for
        # the ones that do (e.g. Spotify), we don't want to touch the real
        # song's position tracking or waste a fetch on the ad's own
        # "artist"/"title" fields. Playback resumes exactly where sync left
        # off once the ad's metadata disappears.
        self.ad_playing = False

    async def _maybe_refine_with_ai(self, song: Song, raw_title: str, raw_artist: str) -> Song:
        """When the regex parser wasn't confident about the artist, ask the
        local Ollama model (if available) to disambiguate uploader-name vs.
        real-artist using the same raw metadata. Never blocks longer than
        song_id_timeout_seconds, and any failure just returns `song` as-is."""
        if not self.song_identifier or song.artist_confidence != "low":
            return song
        try:
            refined = await asyncio.to_thread(self.song_identifier.identify, raw_title, raw_artist)
        except Exception as e:  # local model calls are best-effort, never fatal
            log.debug("AI song identification failed: %s", e)
            return song
        if refined:
            artist, title = refined
            log.info("AI refined %r/%r -> artist=%r title=%r", raw_artist, raw_title, artist, title)
            return Song(artist=artist, title=title, artist_confidence="high")
        return song

    async def preload(self, song: Song, generation: int, force_retry: bool = False,
                       allow_audio_fallback: bool = True) -> None:
        """Off the render loop's critical path: identify -> fetch -> cache.

        Bounded by fetch_timeout_seconds so a slow/hung provider can degrade
        to "no lyrics found" instead of leaving the UI on "loading" forever.
        Only commits results if `generation` is still the newest triggered —
        see the comment on _preload_generation above.

        `allow_audio_fallback` prevents infinite recursion: it's True for
        the normal call path, but False when this call is itself the retry
        triggered by a successful audio-fingerprint identification — so a
        fingerprint-identified song that *still* has no lyrics available
        doesn't loop back into fingerprinting again.
        """
        self.loading = True
        try:
            lines = await asyncio.wait_for(
                asyncio.to_thread(self.lyrics_service.fetch, song, force_retry),
                timeout=self.cfg.fetch_timeout_seconds,
            )
        except asyncio.TimeoutError:
            log.warning("lyric fetch for %r timed out after %.0fs", song, self.cfg.fetch_timeout_seconds)
            lines = []
        except asyncio.CancelledError:
            raise
        except Exception as e:  # any other provider/parse failure: degrade, don't hang or crash
            log.warning("lyric fetch for %r failed: %s", song, e)
            lines = []

        if generation == self._preload_generation:
            self.lyric_lines = lines
            self.lyrics_missing = not lines
            self.loading = False
            # Fast-song tuning (see SyncEngine.adapt_to_lyrics) and the
            # last-line hold duration (see current_and_next) both derive
            # from the same per-song line-gap data — only apply once we
            # actually have it, and only if this is still the current song.
            if len(lines) >= 2:
                gaps = [b.timestamp - a.timestamp for a, b in zip(lines, lines[1:])]
                gaps = [g for g in gaps if g > 0]
                if gaps:
                    self.sync.adapt_to_lyrics(min(gaps))
                    # Hold a line roughly as long as this song's own median
                    # gap suggests a line "occupies", clamped to a sane
                    # range: long enough that normal singing pace never gets
                    # cut off early, short enough that a real instrumental
                    # break or outro clears back to the note placeholder
                    # within a few seconds rather than looking frozen.
                    gaps_sorted = sorted(gaps)
                    median_gap = gaps_sorted[len(gaps_sorted) // 2]
                    self.max_lyric_hold_seconds = max(4.0, min(12.0, median_gap * 1.5))
            if not lines and allow_audio_fallback and self.audio_identifier:
                asyncio.create_task(self._try_audio_fallback(generation))
        else:
            log.debug("discarding stale preload result for %r (generation %d, current %d)",
                      song, generation, self._preload_generation)

    async def _try_audio_fallback(self, generation: int) -> None:
        """Text-based identification found nothing at all — as a last
        resort, record a short sample of whatever's actually playing,
        fingerprint it, and look it up. Only fires once per song (see
        allow_audio_fallback above) and only commits/acts if this is still
        the current song by the time it finishes (same generation guard as
        preload() — a song can easily change during the ~8-15s this takes)."""
        if generation != self._preload_generation:
            return
        self.identifying_by_audio = True
        try:
            identified = await asyncio.to_thread(self.audio_identifier.identify)
        except Exception as e:  # best-effort, never fatal
            log.debug("audio fingerprint identification failed: %s", e)
            identified = None
        finally:
            self.identifying_by_audio = False

        if generation != self._preload_generation or identified is None:
            return
        log.info("audio fingerprint identified: %r", identified)
        self.current_song = identified
        await self.preload(identified, generation, allow_audio_fallback=False)

    async def player_watcher(self, poll_interval: float = 0.5) -> None:
        last_key: Optional[str] = None
        while True:
            reload_requested = _consume_signal(SIGNAL_RELOAD)
            retry_requested = _consume_signal(SIGNAL_RETRY)

            state = await asyncio.to_thread(self.player.read)
            if state is None:
                self.current_song = None
                last_key = None
                self.ad_playing = False
                await asyncio.sleep(poll_interval)
                continue

            if is_advertisement(state.title, state.artist):
                # Don't touch last_key/current_song/sync at all — when the ad
                # ends, the real song's identity and sync position are still
                # exactly where we left them, so playback resumes seamlessly
                # with no re-fetch needed.
                self.ad_playing = True
                await asyncio.sleep(poll_interval)
                continue
            self.ad_playing = False

            song = identify(state.title, state.artist)
            if song.key != last_key or reload_requested or retry_requested:
                song = await self._maybe_refine_with_ai(song, state.title, state.artist)
                last_key = song.key
                self.current_song = song
                self.sync.reset(song.key, state)

                self._preload_generation += 1
                generation = self._preload_generation
                if self._preload_task and not self._preload_task.done():
                    self._preload_task.cancel()
                self.lyric_lines = []  # don't keep showing the previous song's lyrics
                self.lyrics_missing = False
                self.max_lyric_hold_seconds = 8.0  # reset until the new song's own gaps are known

                if self.cfg.preload_before_playback:
                    await self.preload(song, generation, force_retry=retry_requested)
                else:
                    self._preload_task = asyncio.create_task(
                        self.preload(song, generation, force_retry=retry_requested)
                    )
            else:
                self.sync.tick(state, song.key)

            await asyncio.sleep(poll_interval)

    async def render_loop(self, terminal: TerminalSession, keyboard: RawKeyboard) -> None:
        last_line: Optional[str] = None
        last_cols = 0
        last_rows = 0
        typing_target: str = ""    # the full line currently being "typed"
        typing_start = 0.0         # monotonic time the current typing animation began
        last_revealed: Optional[str] = None  # what was actually drawn last, for typing mode
        settings_shown_until = 0.0  # monotonic deadline; see h/? handling below
        was_showing_settings = False
        live_dirty = False  # set True by any keybind — forces an immediate redraw

        # player.read() shells out to the `playerctl` CLI — a real process
        # spawn, not a cheap call. sync.tick() only actually *uses* fresh
        # position data once every poll_interval_seconds internally anyway
        # (see sync.py's tick() — it rate-limits its own drift correction);
        # calling player.read() on every fast render tick was spawning a
        # subprocess ~20x/second for data that was mostly being thrown
        # away, real overhead that could itself cause the render loop to
        # occasionally miss its own tick budget. Poll only this often,
        # reuse the cached state otherwise — tick() still gets called every
        # fast tick either way, so extrapolated position (and therefore
        # line-change responsiveness) stays exactly as fast as before.
        cached_state = None
        last_poll_at = 0.0

        while True:
            size = terminal.size()

            # Non-blocking — never waits for a key, just checks if one's
            # already waiting. See keyboard.py for what each key does.
            key = keyboard.poll()
            if key in BACKSPACE_KEYS:
                self.live.cycle_theme()
                live_dirty = True
            elif key == TAB_KEY:
                self.live.cycle_display_mode()
                live_dirty = True
            elif key == "r":
                self.live.toggle_romanize()
                live_dirty = True
            elif key == "t":
                self.live.toggle_typing_effect()
                live_dirty = True
            elif key == "a":
                self.live.cycle_text_style()
                live_dirty = True
            elif key in ("h", "?"):
                settings_shown_until = time.monotonic() + 4.0
                live_dirty = True

            showing_settings = time.monotonic() < settings_shown_until
            if was_showing_settings and not showing_settings:
                # The 4-second window just lapsed — force one more redraw
                # so the normal lyric line reappears immediately, instead
                # of the overlay staying frozen on screen until the lyric
                # text itself happens to change (which might be a while).
                live_dirty = True
            was_showing_settings = showing_settings

            if showing_settings:
                if live_dirty:
                    frame = self.renderer.build_settings_overlay(size.cols, size.rows)
                    terminal.draw(frame.lines, size)
                    live_dirty = False
                await asyncio.sleep(self.cfg.sync.clock_tick_seconds)
                continue

            state = cached_state
            now = time.monotonic()
            if state is None or (now - last_poll_at) >= self.cfg.sync.poll_interval_seconds:
                state = await asyncio.to_thread(self.player.read)
                cached_state = state
                last_poll_at = now

            if state is None:
                terminal.draw(["[ no player detected ]".center(size.cols)], size)
                await asyncio.sleep(1.0)
                continue

            if self.ad_playing:
                # Deliberately skip self.sync.tick() entirely here — the real
                # song's position tracking stays frozen at exactly where it
                # was when the ad started, so resuming afterward is seamless.
                if last_line != "__ad__":
                    terminal.draw(["[ Advertisement — lyrics paused ]".center(size.cols)], size)
                    last_line = "__ad__"
                await asyncio.sleep(0.5)
                continue

            song = self.current_song
            snapshot = self.sync.tick(state, song.key if song else None)

            if self.loading and not self.lyric_lines:
                msg = f"[ loading: {song.title if song else '...'} ]"
                terminal.draw([msg.center(size.cols)], size)
                await asyncio.sleep(0.1)
                continue

            if self.identifying_by_audio:
                if last_line != "__audio_id__":
                    terminal.draw(["[ listening to identify song... ]".center(size.cols)], size)
                    last_line = "__audio_id__"
                await asyncio.sleep(0.3)
                continue

            if self.lyrics_missing and not self.lyric_lines:
                if last_line != "__no_lyrics__":
                    msg = f"[ no lyrics found: {song.title if song else '...'} ]"
                    terminal.draw([msg.center(size.cols)], size)
                    last_line = "__no_lyrics__"  # sentinel: avoid redrawing every tick
                await asyncio.sleep(0.5)
                continue

            current, _ = current_and_next(
                self.lyric_lines, snapshot.position, max_hold_seconds=self.max_lyric_hold_seconds
            )

            if self.live.typing_effect:
                if current != last_line:
                    # A new line started (or the display went idle) — restart
                    # the typing animation from scratch for it.
                    last_line = current
                    typing_target = current
                    typing_start = time.monotonic()

                if typing_target:
                    elapsed = time.monotonic() - typing_start
                    reveal_count = int(elapsed * self.cfg.render.typing_chars_per_second)
                    revealed = typing_target[: max(0, reveal_count)]
                else:
                    revealed = ""

                if revealed != last_revealed or size.cols != last_cols or size.rows != last_rows or live_dirty:
                    frame = self.renderer.build_frame(revealed, size.cols, size.rows)
                    terminal.draw(frame.lines, size)
                    last_revealed, last_cols, last_rows = revealed, size.cols, size.rows
                    live_dirty = False
            else:
                if current != last_line or size.cols != last_cols or size.rows != last_rows or live_dirty:
                    frame = self.renderer.build_frame(current, size.cols, size.rows)
                    terminal.draw(frame.lines, size)
                    last_line, last_cols, last_rows = current, size.cols, size.rows
                    live_dirty = False

            await asyncio.sleep(self.cfg.sync.clock_tick_seconds)

    async def run(self) -> None:
        with TerminalSession() as terminal, RawKeyboard() as keyboard:
            watcher = asyncio.create_task(self.player_watcher())
            try:
                await self.render_loop(terminal, keyboard)
            finally:
                watcher.cancel()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    config = AppConfig()

    if len(sys.argv) > 1:
        arg = sys.argv[1].strip().lower()
        if arg in ("2", "typing"):
            config = dataclasses.replace(config, render=dataclasses.replace(config.render, typing_effect=True))
        elif arg in ("1", "normal"):
            pass  # already the default
        else:
            print(f"Unknown mode {arg!r} — usage: lyrics [1|2]  (1 = normal, 2 = typing effect)",
                  file=sys.stderr)
            sys.exit(1)

    print(
        "Live keybinds — Backspace: cycle color theme  |  Tab: cycle display mode  |  "
        "a: cycle text style (auto/block/plain)  |  r: toggle romanization  |  "
        "t: toggle typing effect  |  h or ?: show settings",
        file=sys.stderr,
    )

    app = LyricsApp(config)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
