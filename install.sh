#!/usr/bin/env bash
# Installs TermiLyrics into its own virtualenv and links the
# `termilyrics`, `tlyrics` and `lyrics` commands into ~/.local/bin.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${TERMILYRICS_HOME:-$HOME/.local/share/termilyrics}/venv"
BIN="$HOME/.local/bin"

command -v playerctl >/dev/null || echo "warning: playerctl not found — install it (e.g. sudo pacman -S playerctl)" >&2

python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet "$HERE[romanize]" || "$VENV/bin/pip" install --quiet "$HERE"

mkdir -p "$BIN"
for cmd in termilyrics tlyrics lyrics; do
    ln -sf "$VENV/bin/$cmd" "$BIN/$cmd"
done

echo "Installed. Run: termilyrics  (or tlyrics / lyrics)"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: add $BIN to your PATH" >&2 ;; esac
