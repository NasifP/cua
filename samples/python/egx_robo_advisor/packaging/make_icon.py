"""Draw the app icon (the sidebar's "EGX" mark) as a Windows .ico.

    python packaging/make_icon.py build/egx.ico

Generated at build time so the repository carries no binary.
"""

from __future__ import annotations

import sys
from pathlib import Path

ACCENT = (56, 189, 248, 255)   # the dark theme's accent, #38bdf8
INK = (4, 18, 28, 255)         # text on the accent, #04121c
SIZES = [16, 24, 32, 48, 64, 128, 256]
FONTS = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]


def _font(size: int):
    from PIL import ImageFont

    for path in FONTS:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def draw(size: int = 256):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 4, fill=ACCENT)
    font = _font(int(size * 0.36))
    left, top, right, bottom = pen.textbbox((0, 0), "EGX", font=font)
    pen.text(((size - (right - left)) / 2 - left, (size - (bottom - top)) / 2 - top), "EGX",
             font=font, fill=INK)
    return image


def main(argv: list[str]) -> int:
    target = Path(argv[0] if argv else "build/egx.ico")
    target.parent.mkdir(parents=True, exist_ok=True)
    draw(256).save(target, format="ICO", sizes=[(s, s) for s in SIZES])
    print(f"icon: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
