# Reconciliation Notes

Two snapshots of this project got zipped together — a flat set of 13
loose `.py` files, and a `lyrics-sync-redesign.zip` containing an
organized `lyrics_sync/` package. This is what merging them into one
package involved, and how each decision was checked.

## Finding: not a fork — a forward snapshot

`ARCHITECTURE.md` was byte-identical between the two, and every
overlapping file's diff was either pure addition or a coherent
refactor-while-extending (never a competing rewrite of the same
logic). The loose files are a **later** development snapshot of the
exact same design the zip package implements — confirmed by reading
`main.py` in full: the "removed" lines the diff reported were just
old code reshaped to add the generation-guard/audio-fallback/ad-
detection/typing-effect features, not a different approach replacing
a working one.

## What came from where

**From the loose files (current/authoritative for every file present
in both):** `main.py`, `config.py`, `sync.py`, `player.py`,
`detect.py`, `lyrics.py`, `alignment.py`, `ai.py`, `renderer.py`,
`fonts.py`, `terminal.py` (byte-identical either way).

**From the zip package (only these were missing from the loose
snapshot, and both are still actively imported by the current code —
`main.py` imports `TextCache` from `cache.py`, `alignment.py` imports
`TranscriptLine` from `transcript.py`):** `cache.py`, `transcript.py`,
`__init__.py`, `requirements.txt`, `README.md`.

**New in the loose snapshot, not in the zip at all:**
`fingerprint.py` (AcoustID-based last-resort audio identification —
config.py's new `acoustid_*` fields exist specifically to wire this
in) and `spanish.json` (a font pack — moved into `fonts/` alongside
the zip's `german.json`, since `fonts.py` loads every `*.json` in
that directory).

## Verified, not assumed

Both a syntax check and a real import of every module (in dependency
order) pass. Beyond that, three things specifically worth having
actually tested rather than eyeballed:

- **The forward-only alignment invariant** `ARCHITECTURE.md` describes
  — a repeated chorus line resolving to two *different* timestamps,
  never collapsing onto the first occurrence — actually holds when
  the carried-over `transcript.py`'s `TranscriptLine` feeds into the
  current `alignment.py`.
- **`config.py`'s old and new fields coexist** — `transcript_cache`
  (zip-era) and `typing_effect`/`acoustid_api_key` (new) are all
  present and working on the same `AppConfig` instance.
- **The merged font packs** (`german.json` + `spanish.json`) both
  load, and `fonts.py`'s existing `.upper()` fallback in glyph lookup
  means the uppercase-only accented glyphs in `spanish.json` (Á É Í Ó
  Ú Ü Ñ ¿ ¡) correctly resolve for lowercase input too.

Every declared dependency in `requirements.txt` (`syncedlyrics`,
`Pillow`, `PyYAML`, `youtube-transcript-api`) was confirmed still
genuinely used somewhere in the merged codebase, and no new pip
dependency was introduced — `ai.py` and `fingerprint.py` both use only
the standard library plus external binaries (`fpcalc`, `parecord`).
Nothing needed adding to `requirements.txt`.

## What wasn't touched

`ai.py`'s Ollama integration and `fingerprint.py`'s AcoustID
integration weren't tested against real running services (no Ollama
or AcoustID API key in this environment) — only that they degrade
gracefully when unavailable, which is what their own code paths are
designed to do (`available()` checks, try/except around every
external call, `identify()` returning `None` rather than raising).
