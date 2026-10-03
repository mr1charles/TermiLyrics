# 🎵 TermiLyrics

> **A modern terminal lyric synchronizer with animated ASCII vibes.**
>
> **Free. Open Source. Customizable. Beta.**

<p align="center">
  <strong>✨ Your music deserves more than plain text. ✨</strong>
</p>

```
                                         ●
                  ▀▀██▀▀▀ ██   ██ ▀▀██▀▀▀ ██▄  ██ ██  ▄█▀ ██      ██▀▀▀▀▀
                    ██    ██▄█▄██   ██    ██▀█▄██ ██▀▀█▄  ██      ██▀▀▀    ▄▄
                    ▀▀    ▀▀▀ ▀▀▀ ▀▀▀▀▀▀▀ ▀▀   ▀▀ ▀▀   ▀▀ ▀▀▀▀▀▀▀ ▀▀▀▀▀▀▀  ▀▀

                                  How I wonder what you are!

 ♪ Twinkle, Twinkle, Little Star — Jane Taylor  slowed ×0.80  ·  word-sync  ━━●──── 0:08 / 1:23
```

---

## 🚧 Beta Status

> **TermiLyrics is currently in Beta.**

It has been tested on **CachyOS**, but it should work on most modern Linux distributions with the required dependencies.

Please expect occasional bugs, and feel free to open issues or contribute improvements.

---

# ✨ Features

- 🎧 Works with **Spotify**, **YouTube** (in any browser), **mpv**, **VLC**… anything `playerctl` can see
- 🎤 **Sing-Along mode** — a ball bounces from word to word and every word lights up as it's sung
- 🔤 **Word-by-word timing** when available, smart syllable-based estimates when not
- 🐢 **Slowed / sped-up / nightcore tracks line up** — lyrics are automatically re-timed to the track's real speed
- 🎯 **Better lyric matching** — candidates are checked against the song's *length*, so you get the right version (not the live one, not the 10-minute music video)
- 🎨 **6 display modes**, **12 color themes**, **6 line animations**, 3 text sizes
- 🌍 Non-Latin lyrics (Japanese, Chinese, Korean, Cyrillic, …) with optional romanization
- 🎛️ Fix sync on the fly (earlier/later, faster/slower) — remembered per song
- ⚡ Flicker-free rendering, completely **free and open-source**
- 🚀 Built as a modern evolution of **sptlrx**

---

# 📦 Installation

You need **Python 3.9+** and **playerctl** (it's how TermiLyrics talks to your music player).

```bash
# 1. playerctl
sudo pacman -S playerctl          # Arch / CachyOS / Manjaro
sudo apt install playerctl        # Debian / Ubuntu / Mint
sudo dnf install playerctl        # Fedora

# 2. TermiLyrics
git clone https://github.com/mr1charles/TermiLyrics.git
cd TermiLyrics
pip install .                     # or: pipx install .
```

Or use the one-step installer (private virtualenv, no root needed):

```bash
./install.sh
```

Either way you can start it with any of **`termilyrics`**, **`tlyrics`** or **`lyrics`**.

Optional extras:

```bash
pip install ".[romanize]"         # Japanese/Chinese/Korean/Cyrillic → Latin letters
```

> **Tip:** check what TermiLyrics can see with `playerctl -l`. Firefox and Chrome publish their media sessions to it on Linux by default; if your browser doesn't show up, enable its media-keys/MPRIS support (on KDE, the *Plasma Integration* extension does it).

---

# ▶️ How to use it

**1. Play a song** in Spotify, YouTube, or any other player.

**2. Run:**

```bash
termilyrics
```

That's it — TermiLyrics finds the song, fetches synced lyrics and follows along. Change songs, pause, skip, seek: it keeps up.

**Just want to look around first?** No player needed:

```bash
termilyrics --demo                 # built-in demo song with word-by-word timing
termilyrics --demo --speed 0.8     # ...pretending it's a "slowed + reverb" upload
termilyrics --demo my-song.lrc     # play your own .lrc file
```

### 🎤 Sing-along (karaoke) mode

Press **`2`** (or start with `termilyrics --mode karaoke`). A ball hops onto each word right as it's sung, and the words fill with color as you go. The line coming up next is shown underneath, and during instrumental breaks three dots count down to the next line.

### ⌨️ Keys

| Key | What it does |
|---|---|
| **Tab** / **1–6** | Switch display mode (see below) |
| **Backspace** / **c** | Next color theme (**C** = previous) |
| **e** | Next line animation: fade, slide, typewriter, scramble, drop, none |
| **a** | Text size: auto, block (giant), medium, plain |
| **r** | Romanize non-Latin lyrics on/off |
| **p** | Status bar on/off |
| **← / →**  (or **, / .**) | Lyrics **earlier / later** by 0.1s |
| **< / >** | Lyrics earlier / later by 1s |
| **- / +** | Lyrics **slower / faster** (for slowed/sped-up tracks) |
| **v** | Match lyric speed to the track's length (for untagged slowed/sped-up uploads) |
| **0** | Reset sync fixes for this song |
| **f** | Search for lyrics again (wrong or missing lyrics) |
| **Space** / **n** / **b** | Play/pause, next track, previous track |
| **h** / **?** | Show all keys and current settings |
| **q** | Quit |

Your display settings are remembered between runs, and sync fixes are remembered **per song**.

### 🖼️ Display modes

| # | Mode | Looks like |
|---|---|---|
| 1 | **Minimalist** | The current line in giant letters — the classic look |
| 2 | **Sing-Along** | Bouncing ball + words lighting up, previous/next line around it |
| 3 | **Lyrics Sheet** | The whole song scrolling smoothly upward, current line lit word by word |
| 4 | **Word by Word** | Each word pops up giant the moment it's sung |
| 5 | **Music Video Box** | Giant text in a frame with the song title and a progress bar |
| 6 | **Hacker Matrix** | Giant text over falling digital rain |

Themes: `classic_mono`, `cyberpunk_neon`, `monokai`, `cachyos_green_purple`, `sunset`, `ocean`, `dracula`, `catppuccin`, `gruvbox`, `vaporwave`, `matrix_green`, `rainbow` — run `termilyrics --list` to see everything.

### 🐢 Slowed, sped-up & nightcore tracks

Lyric sites only have timings for the *original* song, so a "slowed + reverb" upload normally drifts further and further out of sync. TermiLyrics fixes this automatically:

1. It spots edits in the title — *slowed*, *sped up*, *nightcore*, *daycore*, *chopped & screwed*, *0.8x*, *x1.25*, *85%*…
2. It finds the original song's lyrics **and its length**, compares that to the track you're playing, and stretches the lyrics to match (e.g. 200s original → 250s slowed = ×0.80).
3. If the length isn't known it uses the speed from the title, or a typical value (shown as `≈×0.82` in the status bar).

If it's still off — or the upload isn't labeled at all — press **v** to match the speed to the track length, or nudge it with **- / +**. If the player itself is slowed down (YouTube's playback speed setting), TermiLyrics measures that on its own — nothing to do.

### 🎯 Lyrics out of sync?

- **Always a bit early/late** → **← / →** (saved for that song). For Bluetooth headphones, start with `termilyrics --offset 0.2` to fix every song at once.
- **Getting more and more out of sync** → it's a speed mismatch: **- / +**, or **v**.
- **Wrong lyrics entirely** → **f** to search again.

### 🛠️ Command-line options

```text
termilyrics [--mode MODE] [--theme THEME] [--animation EFFECT] [--style SIZE]
            [--offset SECONDS] [--player NAME] [--fps N] [--no-status] [--no-romanize]
            [--demo [LRC_FILE]] [--speed X] [--color {auto,truecolor,256,16,none}]
            [--acoustid-key KEY] [--reset-settings] [--list] [-v] [--version]
```

`termilyrics 2` still starts with the typing effect, like before. Run `termilyrics --help` for details.

### 🧩 Optional extras

- **Audio recognition** for videos with no useful title: get a free key at [acoustid.org](https://acoustid.org/api-key), install `chromaprint` (for `fpcalc`) and `ffmpeg`, then run with `--acoustid-key KEY` (or set `TERMILYRICS_ACOUSTID_KEY`). It even recognizes slowed/sped-up audio by trying speed corrections.
- **Local AI title cleanup**: if [Ollama](https://ollama.com) is running with `llama3.2:1b` pulled, messy YouTube titles get untangled locally. Nothing leaves your machine.
- **Font packs**: drop JSON glyph packs into `~/Lyrics-Sync/fonts/` (see `fonts/` for examples).

Files live in `~/Lyrics-Sync/` (lyrics cache, settings, per-song sync fixes, and `termilyrics.log` for troubleshooting — use `-v` for more detail).

---

# 🔥 Why TermiLyrics?

TermiLyrics isn't just another lyric viewer.

It's an upgraded, community-friendly reimagining of **sptlrx** that focuses on customization, personality, and making your terminal feel alive.

Whether you're coding, studying, or just vibing with music, your terminal becomes part of the experience.

---

# 🎆 Artist effects

Some artists get their own show. When a track by **BLACKPINK** (or Jennie, Jisoo, Rosé, Lisa) plays, the screen turns into a pink **stage**: moving-head spotlights sweep from the ceiling, the floor glows, and **on every kick drum the lights flare, sparkles pop and the lyrics flash brighter**. Other groups get their own colors — BTS, TWICE, NCT / NCT WISH, Stray Kids, aespa, ITZY, NewJeans, LE SSERAFIM, IVE, (G)I-DLE, EXO, SEVENTEEN, Red Velvet, TXT, ENHYPEN, ATEEZ, MAMAMOO, BIGBANG, Girls' Generation — and Coldplay gets rainbow **confetti** bursts. Run `termilyrics --list` to see all of them.

- **How it hears the beat:** it listens to your system's audio output (the "monitor" of the default sink, through `parec` — present on PulseAudio and PipeWire) and detects kick drums with a small built-in detector. Audio is analysed in memory and never recorded or sent anywhere. If capture isn't possible it pulses on each lyric line instead.
- `x` toggles effects while running; `--no-fx` starts with them off; `--fx blackpink` forces an artist's effect on every song (handy for trying it out: `termilyrics --demo --fx blackpink`).
- Bluetooth speakers lag behind: nudge the flashes later with `--beat-offset 0.15`.
- Add your own artists in `~/Lyrics-Sync/artist_fx.json`:
  ```json
  {"Daft Punk": {"effect": "stage_lights", "colors": ["#00e5ff", "#ff2bd6"]},
   "Halsey":    {"effect": "confetti",     "colors": ["#ff00ff", "#00ffff"]}}
  ```
  Effects: `stage_lights`, `confetti`. Colors are the beam/confetti colors, brightest first.
- Flashes are rate-limited to about 3 a second and use soft colors, never full-screen white — but if you're sensitive to flashing lights, press `x` or use `--no-fx`.

---

# 🛣️ Roadmap

Coming soon:

- 🌍 More lyric providers
- 🎨 Even more visual modes
- ⚙️ A config file for every setting

---

# 🧪 Development

```bash
pip install -e ".[test]"
python -m pytest
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for how the pieces fit together.

---

# 💻 Philosophy

TermiLyrics is built around one simple idea:

> **Music should make your terminal feel alive.**

No subscriptions.

No paywalls.

Just good music, synced lyrics, and terminal aesthetics.

---

# ❤️ Credits

Massive shoutout to:

## 🌟 الو | Alo (Discord)

Thank you for creating and sharing the original foundation that made this project possible.

Your work laid the groundwork for everything that came after. ❤️

Lyrics come from [LrcLib](https://lrclib.net) and the providers supported by [syncedlyrics](https://github.com/moehmeni/syncedlyrics).

---

# 🤖 Fun Fact

This entire project was **vibe coded using Claude and Gemini.**

Sometimes the best software starts with good music, caffeine, and questionable amounts of AI.

---

# 🌟 Support the Project

If you enjoy TermiLyrics:

- ⭐ Star the repository
- 🐛 Report bugs
- 💡 Suggest new features
- 🔧 Submit pull requests
- 🎵 Keep the vibes going

---

<p align="center">

### 🎶 Sync your lyrics. Animate your terminal. Enjoy the music.

**TermiLyrics — because terminals deserve to vibe too.**

</p>
