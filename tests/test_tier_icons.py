import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'source'))

from PIL import Image

from tier_icons import TIER_LABELS, render_tier_icon


class TierIconTests(unittest.TestCase):
    def test_labels_are_drawn_on_the_plain_icon(self):
        source = Image.new('RGBA', (32, 32), (20, 40, 180, 255))
        raw = io.BytesIO()
        source.save(raw, format='PNG')
        pictures = [render_tier_icon(raw.getvalue(), label) for label, _suffix in TIER_LABELS]
        self.assertEqual(len(set(pictures)), 3)
        for picture in pictures:
            image = Image.open(io.BytesIO(picture))
            self.assertEqual(image.size, (128, 128))
            pixels = image.get_flattened_data() if hasattr(image, 'get_flattened_data') else image.getdata()
            whites = sum(1 for red, green, blue, _alpha in pixels if red > 220 and green > 220 and blue > 220)
            self.assertGreater(whites, 20)
