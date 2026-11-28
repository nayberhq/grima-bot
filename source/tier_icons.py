"""Plain class icons with T1, T2, or T* drawn on the picture."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

TIER_LABELS = (('T1', 'T1'), ('T2', 'T2'), ('T*', 'Tstar'))
_FONT_CANDIDATES = (
    '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
    '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
)


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def render_tier_icon(source: bytes, label: str) -> bytes:
    """Return a 128 pixel PNG of the class icon with the tier letters centered on it."""
    image = Image.open(BytesIO(source)).convert('RGBA')
    image = image.resize((128, 128), Image.Resampling.LANCZOS)
    stroke = 10
    scratch = Image.new('RGBA', (320, 240), (0, 0, 0, 0))
    ImageDraw.Draw(scratch).text(
        (12, 12), label, font=_font(120), fill=(255, 255, 255, 255),
        stroke_width=stroke, stroke_fill=(0, 0, 0, 255),
    )
    glyph = scratch.crop(scratch.getbbox())
    glyph = glyph.resize((112, 84), Image.Resampling.LANCZOS)
    image.paste(glyph, ((128 - glyph.width) // 2, (128 - glyph.height) // 2), glyph)
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()
