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
    draw = ImageDraw.Draw(image)
    font = _font(52)
    stroke = 4
    box = draw.textbbox((0, 0), label, font=font, stroke_width=stroke)
    width, height = box[2] - box[0], box[3] - box[1]
    origin_x = (128 - width) / 2 - box[0]
    origin_y = (128 - height) / 2 - box[1]
    draw.text(
        (origin_x, origin_y), label, font=font, fill=(255, 255, 255, 255),
        stroke_width=stroke, stroke_fill=(0, 0, 0, 255),
    )
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()
