import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image

from bowxt.input import X11Clipboard, X11Input
from bowxt.models import Rect


class X11InputMappingTests(unittest.TestCase):
    def test_xtest_event_stays_logical_and_readback_expectation_is_physical(self):
        backend = X11Input.__new__(X11Input)
        backend._xwayland = True
        backend._accessible_window = Rect(288, 135, 730, 650)
        backend.find_wechat_window = lambda: 123
        backend._window_geometry = lambda _window: Rect(576, 270, 1460, 1300)

        self.assertEqual(backend._to_x11_point(605, 214), (605, 214))
        self.assertEqual(backend._expected_physical_point(605, 214), (1210, 428))

    def test_mapping_rejects_point_outside_accessible_window(self):
        backend = X11Input.__new__(X11Input)
        backend._xwayland = True
        backend._accessible_window = Rect(288, 135, 730, 650)
        backend.find_wechat_window = lambda: 123
        backend._window_geometry = lambda _window: Rect(576, 270, 1460, 1300)

        with self.assertRaisesRegex(Exception, "outside the AT-SPI"):
            backend._to_x11_point(100, 100)

    def test_pure_x11_event_is_pre_mapped_to_root_coordinates(self):
        backend = X11Input.__new__(X11Input)
        backend._xwayland = False
        backend._accessible_window = Rect(124, 77, 980, 710)
        backend.find_wechat_window = lambda: 123
        backend._window_geometry = lambda _window: Rect(129, 80, 1021, 740)

        self.assertEqual(backend._to_x11_point(284, 119), (296, 124))
        self.assertEqual(backend._expected_physical_point(284, 119), (296, 124))


class X11ClipboardTests(unittest.TestCase):
    def test_clipboard_png_is_validated_with_original_dimensions(self):
        output = BytesIO()
        Image.new("RGB", (1200, 2670), "green").save(output, format="PNG")
        clipboard = X11Clipboard.__new__(X11Clipboard)

        with patch.object(X11Clipboard, "targets", return_value=("text/plain", "image/png")), \
             patch.object(
                 X11Clipboard,
                 "read_bytes",
                 side_effect=lambda mime: output.getvalue() if mime == "image/png" else None,
             ):
            image = clipboard.read_image()

        self.assertIsNotNone(image)
        self.assertEqual((image.width, image.height), (1200, 2670))
        self.assertEqual(image.mime_type, "image/png")
        self.assertEqual(image.source, "viewer_clipboard")

    @patch("bowxt.input.shutil.which", return_value="/usr/bin/xclip")
    @patch.dict("bowxt.input.os.environ", {"DISPLAY": ":1"}, clear=False)
    @patch("bowxt.input.subprocess.run")
    def test_temporary_text_is_restored(self, run, _which):
        run.return_value.returncode = 0
        run.return_value.stdout = "原内容".encode()
        clipboard = X11Clipboard(restore=True)

        with clipboard.text("测试消息"):
            pass

        writes = [call.kwargs["input"].decode() for call in run.call_args_list if "input" in call.kwargs]
        self.assertEqual(writes, ["测试消息", "原内容"])
