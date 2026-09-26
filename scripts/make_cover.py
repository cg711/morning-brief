"""Generate the 1400x1400 podcast covers. Run: .venv/bin/python scripts/make_cover.py"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W = 1400
STATIC = Path(__file__).resolve().parent.parent / "morning_brief" / "static"


def make(out: Path, top, bottom, lines) -> None:
    img = Image.new("RGB", (W, W))
    draw = ImageDraw.Draw(img)
    for y in range(W):
        t = y / (W - 1)
        draw.line([(0, y), (W, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    draw.ellipse([W * 0.3, W * 0.52, W * 0.7, W * 0.92], fill=(255, 237, 213))
    draw.rectangle([0, W * 0.72, W, W], fill=(29, 27, 24))
    font = ImageFont.load_default(size=170)
    for n, text in enumerate(lines):
        draw.text((W / 2, W * (0.24 + 0.14 * n)), text, font=font, anchor="mm", fill="white")
    img.save(out, optimize=True)
    print(out)


make(STATIC / "cover.png", (194, 65, 12), (251, 146, 60), ["Morning", "Brief"])
make(STATIC / "deep-dives-cover.png", (30, 58, 138), (96, 165, 250), ["Deep", "Dives"])
