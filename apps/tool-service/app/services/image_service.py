import io

from PIL import Image


def process_image(
    data: bytes,
    max_width: int | None = None,
    max_height: int | None = None,
    quality: int = 85,
    format: str | None = None,
) -> tuple[bytes, int, int]:
    """
    通用图片处理：缩放 + 格式转换。
    返回 (output_bytes, width, height)
    """
    img = Image.open(io.BytesIO(data))

    # 缩放（thumbnail 不放大，等比缩放到 max_width x max_height 以内）
    if max_width or max_height:
        w = max_width or img.width
        h = max_height or img.height
        img.thumbnail((w, h), Image.LANCZOS)

    # 输出
    out_format = format or img.format or "JPEG"
    buf = io.BytesIO()
    save_kwargs: dict = {}
    if out_format.upper() == "JPEG":
        img = img.convert("RGB")
        save_kwargs = {"quality": quality, "progressive": True}
    elif out_format.upper() == "WEBP":
        save_kwargs = {"quality": quality}
    img.save(buf, format=out_format, **save_kwargs)
    return buf.getvalue(), img.width, img.height
