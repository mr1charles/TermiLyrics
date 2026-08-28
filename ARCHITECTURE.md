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
transcript.py, alignment.py, fonts.py, renderer.py, ai.py, main.py` — each
owns one concern, communicates through small dataclasses
(`PlayerState`, `Song`, `LyricLine`, `AlignedLine`, `SyncSnapshot`), and has no
module-level mutable state.

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

## 3. Migration plan

1. Drop `lyrics_sync/` next to the old `lyrics-sync.py`; keep the old file
   untouched until the new one is verified.
2. `pip install -r requirements.txt` (Pillow/PyYAML are already effectively
   required for the font engine to be useful; the rest are optional extras).
3. Run `python -m lyrics_sync.main` instead of `python lyrics-sync.py`.
4. Cache layout changed slightly (`~/Lyrics-Sync/lyrics/*.lrc` instead of
   `~/Lyrics-Sync/*.lrc`) — old cached files will simply be re-scraped once;
   nothing needs manual migration.
5. Signal files (`/tmp/lyrics_force_reload`, `/tmp/lyrics_force_retry`) and the
   Caelestia LRC mirror behavior are preserved as-is, so existing
   `mysong-fix`/`mysong-retry` scripts keep working unchanged.
6. Font packs are optional and additive — the app works identically with none
   installed (falls straight to the Pillow raster fallback for anything not in
   the base Latin table).
