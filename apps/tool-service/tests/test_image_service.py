import io

import pytest
from PIL import Image

from app.services.image_service import process_image


def _make_image(width: int, height: int, fmt: str = "PNG", mode: str = "RGB") -> bytes:
    """生成测试用图片"""
    img = Image.new(mode, (width, height), color="red")
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


class TestProcessImage:
    def test_passthrough(self):
        """无参数时保持原尺寸"""
        data = _make_image(800, 600, "PNG")
        out, w, h = process_image(data)
        assert w == 800
        assert h == 600

    def test_downscale(self):
        """缩小到 max_width x max_height 以内"""
        data = _make_image(2000, 1000)
        out, w, h = process_image(data, max_width=500, max_height=500)
        assert w <= 500
        assert h <= 500
        # 保持宽高比
        assert w == 500
        assert h == 250

    def test_no_upscale(self):
        """thumbnail 不会放大小图"""
        data = _make_image(100, 50)
        out, w, h = process_image(data, max_width=500, max_height=500)
        assert w == 100
        assert h == 50

    def test_format_conversion_to_jpeg(self):
        """PNG 转 JPEG"""
        data = _make_image(100, 100, "PNG")
        out, w, h = process_image(data, format="JPEG")
        result = Image.open(io.BytesIO(out))
        assert result.format == "JPEG"

    def test_format_conversion_to_webp(self):
        """转 WEBP"""
        data = _make_image(100, 100, "PNG")
        out, w, h = process_image(data, format="WEBP")
        result = Image.open(io.BytesIO(out))
        assert result.format == "WEBP"

    def test_jpeg_quality(self):
        """低 quality 生成更小的文件"""
        data = _make_image(500, 500)
        out_high, _, _ = process_image(data, format="JPEG", quality=95)
        out_low, _, _ = process_image(data, format="JPEG", quality=10)
        assert len(out_low) < len(out_high)

    def test_max_width_only(self):
        """仅指定 max_width"""
        data = _make_image(2000, 1000)
        out, w, h = process_image(data, max_width=500)
        assert w == 500
        assert h == 250

    def test_max_height_only(self):
        """仅指定 max_height"""
        data = _make_image(2000, 1000)
        out, w, h = process_image(data, max_height=250)
        assert w == 500
        assert h == 250

    def test_rgba_to_jpeg(self):
        """RGBA 图片转 JPEG 时自动转 RGB"""
        data = _make_image(100, 100, "PNG", mode="RGBA")
        out, w, h = process_image(data, format="JPEG")
        result = Image.open(io.BytesIO(out))
        assert result.mode == "RGB"
        assert result.format == "JPEG"
