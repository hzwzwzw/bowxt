from __future__ import annotations

import io
import math
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass

from .input import X11Input
from .models import Direction, MessageImage, Rect


@dataclass(slots=True)
class VisualSnapshot:
    """Ephemeral WeChat-window pixels used for explicitly enabled enrichment."""

    image: object
    logical_window: Rect

    def crop_logical(self, rect: Rect, *, width_ratio: float = 1.0, height_ratio: float = 1.0):
        """Crop a logical desktop rectangle from the in-memory physical image."""

        image = self.image
        scale_x = image.width / self.logical_window.width
        scale_y = image.height / self.logical_window.height
        left = max(0, round((rect.x - self.logical_window.x) * scale_x))
        top = max(0, round((rect.y - self.logical_window.y) * scale_y))
        right = min(
            image.width,
            round((rect.x + rect.width * width_ratio - self.logical_window.x) * scale_x),
        )
        bottom = min(
            image.height,
            round((rect.y + rect.height * height_ratio - self.logical_window.y) * scale_y),
        )
        if right <= left or bottom <= top:
            return None
        return image.crop((left, top, right, bottom))

    def classify(self, row: Rect) -> Direction:
        crop = self.crop_logical(row)
        if crop is None or crop.width < 20 or crop.height < 8:
            return Direction.UNKNOWN
        crop = crop.convert("RGB")
        pixel_source = (
            crop.get_flattened_data() if hasattr(crop, "get_flattened_data") else crop.getdata()
        )
        quantized = Counter((r // 8, g // 8, b // 8) for r, g, b in pixel_source)
        if not quantized:
            return Direction.UNKNOWN
        background_bin, _count = quantized.most_common(1)[0]
        background = tuple(value * 8 + 4 for value in background_bin)
        foreground = [0, 0]
        step = 2
        for y in range(0, crop.height, step):
            for x in range(0, crop.width, step):
                pixel = crop.getpixel((x, y))
                distance = math.sqrt(sum((pixel[index] - background[index]) ** 2 for index in range(3)))
                if distance > 28:
                    foreground[int(x >= crop.width / 2)] += 1
        left_count, right_count = foreground
        total = left_count + right_count
        if total < 40:
            return Direction.UNKNOWN
        if right_count > max(40, left_count * 2.2):
            return Direction.OUTGOING
        if left_count > max(40, right_count * 2.2):
            return Direction.INCOMING
        return Direction.UNKNOWN

    def read_image(self, rect: Rect) -> MessageImage | None:
        """Encode the visible image bubble as PNG; no OCR or app internals are used."""

        crop = self.crop_logical(rect)
        if crop is None or crop.width < 2 or crop.height < 2:
            return None
        output = io.BytesIO()
        crop.save(output, format="PNG", optimize=True)
        return MessageImage(
            data=output.getvalue(),
            mime_type="image/png",
            width=crop.width,
            height=crop.height,
            source="window_pixels",
        )

    def locate_image(self, row: Rect) -> Rect | None:
        """Locate the largest non-background block in an image message row."""

        crop = self.crop_logical(row)
        if crop is None or crop.width < 8 or crop.height < 8:
            return None
        crop = crop.convert("RGB")
        pixels = list(
            crop.get_flattened_data() if hasattr(crop, "get_flattened_data") else crop.getdata()
        )
        quantized = Counter((red // 8, green // 8, blue // 8) for red, green, blue in pixels)
        if not quantized:
            return None
        background_bin, _count = quantized.most_common(1)[0]
        background = tuple(value * 8 + 4 for value in background_bin)
        width, height = crop.size
        threshold_squared = 24 * 24
        foreground = bytearray(
            int(sum((pixel[index] - background[index]) ** 2 for index in range(3)) > threshold_squared)
            for pixel in pixels
        )
        seen = bytearray(width * height)
        components: list[tuple[int, int, int, int, int]] = []
        for start, is_foreground in enumerate(foreground):
            if not is_foreground or seen[start]:
                continue
            stack = [start]
            seen[start] = 1
            pixel_count = 0
            min_x, min_y, max_x, max_y = width, height, 0, 0
            while stack:
                current = stack.pop()
                y, x = divmod(current, width)
                pixel_count += 1
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
                for next_x, next_y in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                    if not (0 <= next_x < width and 0 <= next_y < height):
                        continue
                    neighbor = next_y * width + next_x
                    if foreground[neighbor] and not seen[neighbor]:
                        seen[neighbor] = 1
                        stack.append(neighbor)
            box_width, box_height = max_x - min_x + 1, max_y - min_y + 1
            box_area = box_width * box_height
            if (
                pixel_count >= 100
                and min(box_width, box_height) >= 32
                and max(box_width, box_height) >= 64
                and pixel_count / box_area >= 0.02
            ):
                components.append((box_area, min_x, min_y, max_x + 1, max_y + 1))
        if not components:
            return None
        _area, left, top, right, bottom = max(components)
        scale_x = self.image.width / self.logical_window.width
        scale_y = self.image.height / self.logical_window.height
        return Rect(
            row.x + round(left / scale_x),
            row.y + round(top / scale_y),
            max(1, round((right - left) / scale_x)),
            max(1, round((bottom - top) / scale_y)),
        )


class VisualDirectionDetector:
    """Window capture used by opt-in visual enrichment; persists no pixels."""

    @staticmethod
    def available() -> bool:
        if not shutil.which("import"):
            return False
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            return False
        return True

    def capture(self, logical_window: Rect) -> VisualSnapshot:
        if not self.available():
            raise RuntimeError("visual fallback requires ImageMagick `import` and Pillow")
        from PIL import Image

        x11 = X11Input()
        try:
            window = x11.find_wechat_window()
        finally:
            x11.close()
        result = subprocess.run(
            ["import", "-silent", "-window", hex(window), "png:-"],
            check=True,
            capture_output=True,
            timeout=5,
        )
        image = Image.open(io.BytesIO(result.stdout)).copy()
        return VisualSnapshot(image=image, logical_window=logical_window)
