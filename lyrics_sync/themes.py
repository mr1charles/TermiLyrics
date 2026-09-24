"""Color themes and small color math helpers."""
from __future__ import annotations

import colorsys
from dataclasses import dataclass
from typing import Dict, Tuple

RGB = Tuple[int, int, int]


@dataclass(frozen=True)
class ColorTheme:
    name: str
    primary: RGB      # giant text, top of the gradient
    secondary: RGB    # bottom of the gradient, accents
    # Sung words, the bouncing ball, the current line in list views.
    highlight: RGB = (255, 255, 255)
    # Context lines (previous/next), unsung words' shade, status bar.
    muted: RGB = (110, 110, 110)
    # Hue rotates over time (the "rainbow" theme).
    animated: bool = False


# Keys here are what RenderConfig.theme / --theme reference.
THEMES: Dict[str, ColorTheme] = {
    "classic_mono": ColorTheme("Classic Monochrome", (225, 225, 225), (225, 225, 225),
                               (255, 255, 255), (105, 105, 105)),
    "cyberpunk_neon": ColorTheme("Cyberpunk Neon", (255, 0, 170), (0, 255, 255),
                                 (0, 255, 255), (125, 95, 150)),
    "monokai": ColorTheme("Monokai", (249, 38, 114), (166, 226, 46), (230, 219, 116), (117, 113, 94)),
    "cachyos_green_purple": ColorTheme("CachyOS Green/Purple", (148, 0, 211), (0, 255, 127),
                                       (0, 255, 127), (110, 90, 130)),
    "sunset": ColorTheme("Sunset", (255, 94, 98), (255, 195, 113), (255, 230, 120), (150, 95, 95)),
    "ocean": ColorTheme("Ocean", (0, 150, 220), (72, 220, 228), (175, 240, 255), (70, 105, 135)),
    "dracula": ColorTheme("Dracula", (189, 147, 249), (255, 121, 198), (80, 250, 123), (98, 114, 164)),
    "catppuccin": ColorTheme("Catppuccin Mocha", (203, 166, 247), (137, 180, 250),
                             (249, 226, 175), (108, 112, 134)),
    "gruvbox": ColorTheme("Gruvbox", (254, 128, 25), (250, 189, 47), (184, 187, 38), (146, 131, 116)),
    "vaporwave": ColorTheme("Vaporwave", (255, 113, 206), (1, 205, 254), (185, 103, 255), (120, 100, 150)),
    "matrix_green": ColorTheme("Matrix Green", (0, 255, 70), (0, 160, 40), (180, 255, 180), (0, 110, 30)),
    "rainbow": ColorTheme("Rainbow (animated)", (255, 80, 80), (80, 80, 255), (255, 255, 255),
                          (110, 110, 110), animated=True),
}
DEFAULT_THEME = THEMES["classic_mono"]
THEME_KEYS: Tuple[str, ...] = tuple(THEMES.keys())


def lerp(a: RGB, b: RGB, t: float) -> RGB:
    t = 0.0 if t < 0 else 1.0 if t > 1 else t
    return (int(a[0] + (b[0] - a[0]) * t), int(a[1] + (b[1] - a[1]) * t), int(a[2] + (b[2] - a[2]) * t))


def hue(h: float, s: float = 0.85, v: float = 1.0) -> RGB:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return int(r * 255), int(g * 255), int(b * 255)


# What we fade in from / out to. Terminals don't report their background
# color, and dark backgrounds are the overwhelming majority for this kind of
# app, so fades go through near-black.
BACKDROP: RGB = (12, 12, 16)
