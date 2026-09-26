# lyrics-sync — Redesign Notes

## 1. What's wrong with the current single-file version

**Timing is fragile.** `get_current_and_next` re-derives the current lyric from
`playerctl position` on every 0.1s tick, and that call is trusted completely.
Playerctl goes through D-Bus to the player, which means every read carries
process-scheduling and IPC jitter — a few hundred ms of noise is normal. Because
nothing smooths or filters that value, the giant text visibly jumps, occasionally
snaps backward for one frame, and freezes for a beat when the player itself
stutters (buffering, browser tab throttling). There's also no concept of
"paused" vs "still playing at the same position" — a stall reads identically to
a pause.

**Alignment doesn't exist yet.** The current code trusts whatever timestamps
ship in the scraped LRC file. When a provider's timestamps are wrong (common
for auto-generated or fan-submitted LRC), there's no correction mechanism.

**Song identification is a single regex.** `clean_title()` strips one
"` - `"-delimited suffix and two `(feat...)` patterns. It has no concept of
uploader-prefixed titles ("7clouds - Artist - Song"), YouTube Music's
song-first ordering, or browser-chrome suffixes ("- Mozilla Firefox").

**The font is a hardcoded 45-entry dict.** Anything outside `A-Z0-9` and a
handful of punctuation marks is silently dropped by `render_block_rows`
(`if glyph is None: continue`) — accented Latin, let alone CJK/Arabic/
Devanagari, just vanishes character by character.

**Everything is one file, synchronous, top to bottom.** Song detection,
network I/O, caching, rendering, and the main loop are all interleaved in
`main()`. There's no way to preload the next song's lyrics while the current
one is still playing, and a slow network call blocks the render loop.

## 2. Redesign

### 2.1 Sync engine (`sync.py`) — Problem 1

The core fix: **stop asking playerctl for continuous position.** Instead:

- `SyncEngine` keeps a local monotonic clock checkpoint `(pos_ref, wall_ref)`.
  Between polls, position is *extrapolated* (`pos_ref + elapsed`), which is
  smooth by construction — no IPC jitter in the render path at all.
- Playerctl is polled on a slower cadence (`poll_interval_seconds`, default
  0.5s) purely to correct drift.
- Two drift bands, both configurable:
  - small drift (buffering, clock skew) → **half-step smoothing**, so the
    correction is invisible: `pos_ref = estimate + drift * 0.5`.
  - large drift (`seek_jump_threshold`) → **instant snap**, because that's a
    real seek/rewind/skip and smoothing it would look laggy.
- Play/pause transitions are read from `status`, not inferred from position,
  and applied the instant they're seen — no waiting for the next poll tick.
- A song-key change (from `detect.py`) forces a hard `reset()`, so ad breaks
  and track skips can't leave stale state (drift correction, cached lyric
  index) bleeding into the next song.

### 2.2 Alignment (`alignment.py`) — Problem 2

`ForwardAligner` matches each LRC line to a transcript line using a blended
score (`0.5 * token_overlap + 0.5 * SequenceMatcher.ratio`), but the search is
constrained to be **forward-only and non-reusing**: the transcript cursor only
moves forward, and a matched index is never revisited. That single invariant
is what makes repeated chorus lines resolve to *different* timestamps instead
of all snapping to the first occurrence, and guarantees lyric ordering can
never invert. Low-confidence lines fall back to the original LRC timestamp
(if still forward-consistent) or a small interpolated offset — never silently
dropped.

### 2.3 Song identification (`detect.py`) — Problem 3

A small rules engine instead of one regex: strips browser chrome
("- Mozilla Firefox", "- 3 more tabs"), strips noise tags
(`(Official Video)`, `(Lyrics)`, etc.) and `feat.` clauses, recognizes known
lyric-video uploader prefixes ("7clouds"), and only falls back to splitting on
`" - "` when the player didn't already give a clean artist field (Spotify/MPV/
VLC via playerctl do; browser tab titles don't). `source` hints let it prefer
YouTube Music's song-first convention when known.

### 2.4 Font engine (`fonts.py`) — Problem 4

Hand-authoring glyphs for Chinese, Japanese, Korean, Arabic, Hebrew, Devanagari
and Thai isn't practical or maintainable as static tables. Instead:

1. **`StaticBlockFont`** — the existing hand-tuned 5-row glyphs, extendable by
   dropping JSON/YAML packs (`{"glyphs": {"Ä": [...]}}`) into the fonts
   directory. Best visual quality, used first, covers Latin scripts including
   German/French/Spanish/Portuguese/Italian diacritics once packs are added.
2. **`RasterUnicodeFont`** — for anything the static table doesn't cover, it
   rasterizes the actual character with Pillow against a system TrueType font
   and downsamples the bitmap to block-shade characters. This is what makes
   the "support every script" requirement tractable: any character a system
   font can draw, we can turn into a giant terminal glyph, with no per-script
   code. Results are cached to disk keyed by `(char, height)`.
3. **Literal fallback** — if even Pillow is unavailable, the raw character is
   centered in the glyph box rather than being dropped.

`FontEngine` tries sources in that order, so nothing is ever skipped.

### 2.5 Performance / preload (`main.py`) — Problem 5

`asyncio`-based: `player_watcher()` and `render_loop()` run as separate tasks.
On song change, `preload()` (identify → fetch lyrics → cache) runs via
`asyncio.to_thread` so it never blocks rendering; the render loop shows a
"loading" state until it completes. `preload_before_playback` in config
switches to the blocking variant for people who'd rather wait than see a
loading placeholder.

### 2.6 Modules — Problem 6

`config.py, terminal.py, player.py, sync.py, detect.py, cache.py, lyrics.py,
timing.py, transcript.py, alignment.py, fonts.py, canvas.py, themes.py,
renderer.py, keyboard.py, settings.py, demo.py, fingerprint.py, ai.py,
main.py` — each owns one concern and communicates through small dataclasses
(`PlayerState`, `Song`, `LyricLine`/`Word`, `LyricsResult`, `Cursor`,
`TempoMap`, `Scene`, `SyncSnapshot`). The only module-level mutable state is
the terminal color mode, set once at startup.

### 2.7 Renderer (`renderer.py`) — Problem 7

Centering and word-wrap now measure actual glyph widths from the font engine
(so it works regardless of which font source drew a character), with gradient
and rainbow color modes (per-column hue sweep or per-row gradient), optional
outline/shadow effects, and terminal-resize handled by re-measuring on every
tick (cheap) rather than caching stale dimensions.

### 2.8 Local-only AI (`ai.py`) — Problem 8

`LocalAlignmentAssist` wraps `sentence-transformers` (a local embedding model,
no network calls after the model is cached) to give `alignment.py` an optional
extra similarity signal for heavily reworded transcripts. It's entirely
optional: if the package isn't installed, `enabled` goes `False` and every
caller gets `None` back, never an exception. Nothing in the app requires it.

### 2.9 Reliability — Problem 9

- `cache.py` writes are atomic (`tempfile` + `os.replace`) so a crash mid-write
  can't corrupt a cache entry; reads that hit corrupted/undecodable files
  evict and re-fetch rather than crashing or serving garbage.
- Network/provider failures in `lyrics.py` and `transcript.py` are caught
  per-call and degrade to "no lyrics yet" rather than crashing the loop.
- Missing metadata (`title` empty) is treated as "no player" instead of
  raising.
- Multiple players: `PlayerSource.read()` walks preferred → all `playerctl -l`
  players → default, skipping any that error.

## 3. v0.1 upgrade

### 3.1 Player reads (`player.py`)
One `playerctl --all-players metadata --format …` process per poll covers
every player (the old code spawned five processes *per player*), and every
`PlayerState` carries `sampled_at` — the monotonic time its position was
true (midpoint of the subprocess call). Among several players: playing beats
paused, then the player we were already following, then `--player` prefs.

### 3.2 Sync engine (`sync.py`)
Extrapolation is anchored at the *sample* time, not the time the reading
was processed, so song identification or an AI call between read and reset
no longer shifts the clock. Playback rate is learned by least squares over
the last ~8s of readings (snapped to common speeds; a poor fit discards the
older half so a speed change is picked up within seconds). The correction
dead-band is 3x the *spread* (MAD) of recent drift — a steady offset has no
spread, so it is always corrected; real jitter is ignored. `position()`
holds still instead of stepping backwards for sub-0.3s corrections.
Simulated against noisy 0.75x/1.0x/1.25x players, error stays under 0.1s.

### 3.3 Lyric finder (`lyrics.py`)
- Parser: multiple timestamps per line, `[offset:]`, `[length:]`, `[mm:ss:xx]`,
  enhanced word tags (`<mm:ss.xx>`, both A2 and syncedlyrics' richsync
  spacing), per-character CJK timing, credit/URL line removal.
- LrcLib is queried directly and candidates are scored on title (noise-
  stripped, accent-folded), artist, and **duration** vs. the playing track,
  with penalties for live/remix/instrumental/etc. versions the title didn't
  ask for. syncedlyrics runs second, `synced_only=True` (plain lyrics used
  to end the search), and results longer than the track are rejected.
- A JSON sidecar per song caches duration/source, and a 12h negative cache
  (only when LrcLib answered cleanly — never on a network error).
- Word-level timing (Musixmatch richsync via syncedlyrics) is fetched in
  the background after line-synced lyrics are showing, and only swapped in
  if `lyrics_match()` confirms same text and timing within ~2s.

### 3.4 Timeline (`timing.py`)
`LyricTimeline` precomputes per-word times — real ones, or estimates spread
over the time a line is plausibly *sung* (syllable-weighted, ~2–5
syllables/s) rather than the whole gap to the next line — and `locate(t)`
returns a `Cursor` (line, word, progress, gap countdown) in O(log n).

### 3.5 Slowed / sped-up tracks (`detect.py`, `timing.py`, `fingerprint.py`)
`detect_variant()` reads the raw title (slowed, sped up, nightcore, daycore,
chopped & screwed, `0.8x`, `x1.25`, `85%`). `Song.key` (cache) ignores the
variant; `Song.identity` (track change) includes it. `variant_scale()` picks
the lyric-time scale: original length / track length when both are known and
agree with the tag, else the title's factor, else a typical value flagged as
a guess. Untagged tracks are only stretched on request (`v`), since a music
video's intro also makes lengths differ. `TempoMap` applies
`lyric_time = position * scale + offset`. Audio fingerprinting retries at
resample factors (ffmpeg `asetrate`) so slowed audio matches the original
recording, and the matching factor *is* the track's speed.

### 3.6 Rendering (`canvas.py`, `renderer.py`, `terminal.py`)
Every mode paints into a `Canvas` cell grid (wide/combining-character aware)
serialized once per frame; `TerminalSession.draw` rewrites only changed rows
on the alternate screen (no full clear → no flicker). Colors degrade from
truecolor to 256/16/none. Six modes (minimalist, karaoke, scroll, word_pop,
box, matrix), six transitions, a half-block "medium" text size between giant
and plain, and `Frame.animating` so static frames aren't redrawn — the app
loop only runs at `fps` while something moves.

### 3.7 App (`main.py`, `keyboard.py`, `settings.py`, `demo.py`)
One poller feeds the sync engine; the render loop reads keys (escape
sequences parsed from raw fd reads) and renders. Blocking network work runs
in daemon threads so a stuck provider can't hang quitting. Live settings and
per-track sync adjustments persist as JSON. `--demo` swaps in a local player
and a built-in word-timed song.

## 4. Migration plan

1. Drop `lyrics_sync/` next to the old `lyrics-sync.py`; keep the old file
   untouched until the new one is verified.
2. `pip install -r requirements.txt` (Pillow/PyYAML are already effectively
   required for the font engine to be useful; the rest are optional extras).
3. Run `termilyrics` (or `python -m lyrics_sync`) instead of `python lyrics-sync.py`.
4. Cache layout changed slightly (`~/Lyrics-Sync/lyrics/*.lrc` instead of
   `~/Lyrics-Sync/*.lrc`) — old cached files will simply be re-scraped once;
   nothing needs manual migration.
5. Signal files (`/tmp/lyrics_force_reload`, `/tmp/lyrics_force_retry`) and the
   Caelestia LRC mirror behavior are preserved as-is, so existing
   `mysong-fix`/`mysong-retry` scripts keep working unchanged.
6. Font packs are optional and additive — the app works identically with none
   installed (falls straight to the Pillow raster fallback for anything not in
   the base Latin table).
