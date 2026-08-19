import unittest

from bowxt.models import Direction, Rect


class VisionTests(unittest.TestCase):
    def test_geometry_fallback_classifies_left_and_right_bubbles(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow is optional")
        from bowxt.vision import VisualSnapshot

        right = Image.new("RGB", (200, 80), (28, 28, 28))
        ImageDraw.Draw(right).rectangle((125, 15, 195, 65), fill=(45, 180, 95))
        left = Image.new("RGB", (200, 80), (28, 28, 28))
        ImageDraw.Draw(left).rectangle((5, 15, 75, 65), fill=(80, 80, 80))
        window = Rect(0, 0, 200, 80)

        self.assertEqual(VisualSnapshot(right, window).classify(window), Direction.OUTGOING)
        self.assertEqual(VisualSnapshot(left, window).classify(window), Direction.INCOMING)
