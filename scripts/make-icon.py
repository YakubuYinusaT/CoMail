"""Build Cohandle's icon from its wordmark.

    python scripts/make-icon.py

Cohandle has no logo of its own, so the desktop icon was borrowing
Connected Intelligence's wing. This draws the product's own: the wordmark
cut to its first two letters, white on brand red.

A red field rather than a red mark on white, because the complaint that
started this was a taskbar icon nobody could see against a dark taskbar.
A solid block reads at sixteen pixels. Hairlines on transparency do not.

Writes app/static/cohandle.ico, which both the Windows shortcut and the
page favicon point at.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = ROOT / "app/static/fonts/Poppins-SemiBold.ttf"
ICO = ROOT / "app/static/cohandle.ico"
PNG = ROOT / "app/static/cohandle-mark.png"

RED = (245, 0, 0, 255)          # --red, F50000
WHITE = (255, 255, 255, 255)
TEXT = "co"
SIZES = [16, 24, 32, 48, 64, 128, 256]


def tile(side: int) -> Image.Image:
    img = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([0, 0, side - 1, side - 1],
                           radius=max(2, round(side * 0.22)), fill=RED)

    # Largest size that still leaves a margin, found by trying rather than
    # by a ratio: Poppins' metrics do not scale to a neat fraction.
    size = round(side * 0.8)
    while size > 4:
        font = ImageFont.truetype(str(FONT), size)
        box = draw.textbbox((0, 0), TEXT, font=font)
        if box[2] - box[0] <= side * 0.72 and box[3] - box[1] <= side * 0.6:
            break
        size -= 1

    box = draw.textbbox((0, 0), TEXT, font=font)
    draw.text(((side - (box[2] + box[0])) / 2, (side - (box[3] + box[1])) / 2),
              TEXT, font=font, fill=WHITE)
    return img


def main() -> int:
    frames = [tile(s) for s in SIZES]
    frames[-1].save(ICO, format="ICO", sizes=[(s, s) for s in SIZES])
    tile(512).save(PNG)
    print(f"wrote {ICO.relative_to(ROOT)} and {PNG.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
