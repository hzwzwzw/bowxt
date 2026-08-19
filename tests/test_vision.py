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

    def test_visible_image_crop_is_encoded_as_png(self):
        try:
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow is optional")
        from bowxt.vision import VisualSnapshot

        source = Image.new("RGB", (200, 100), (20, 30, 40))
        snapshot = VisualSnapshot(source, Rect(100, 50, 200, 100))
        image = snapshot.read_image(Rect(150, 70, 80, 40))

        self.assertIsNotNone(image)
        self.assertEqual((image.width, image.height), (80, 40))
        self.assertEqual(image.mime_type, "image/png")
        self.assertTrue(image.data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_image_locator_selects_picture_instead_of_avatar(self):
        try:
            from PIL import Image, ImageDraw
        except ImportError:
            self.skipTest("Pillow is optional")
        from bowxt.vision import VisualSnapshot

        source = Image.new("RGB", (500, 180), (238, 238, 238))
        drawing = ImageDraw.Draw(source)
        drawing.rectangle((20, 20, 59, 59), fill=(80, 120, 160))
        drawing.rectangle((80, 20, 259, 149), fill=(40, 90, 140))

        located = VisualSnapshot(source, Rect(0, 0, 500, 180)).locate_image(
            Rect(0, 0, 500, 180)
        )

        self.assertEqual(located, Rect(80, 20, 180, 130))
