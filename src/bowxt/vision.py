from __future__ import annotations

import io
import math
import shutil
import subprocess
from collections import Counter
from dataclasses import dataclass

from .input import X11Input
from .models import Direction, Rect


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
