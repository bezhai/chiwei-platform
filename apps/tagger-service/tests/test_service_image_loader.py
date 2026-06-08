from __future__ import annotations

from io import BytesIO

from PIL import Image

from app.service.image_loader import load_images_from_paths


class FakeReader:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    def read_object(self, key: str) -> bytes:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key]


def _image_bytes() -> bytes:
    buf = BytesIO()
    Image.new("RGB", (8, 8), (20, 30, 40)).save(buf, format="PNG")
    return buf.getvalue()


def test_load_images_from_paths_returns_items_and_error_rows() -> None:
    loaded = load_images_from_paths(
        ["ok.png", "missing.png", "bad.png"],
        FakeReader({"ok.png": _image_bytes(), "bad.png": b"not an image"}),
    )

    assert [image_id for image_id, _ in loaded.items] == ["ok.png"]
    assert [row["id"] for row in loaded.error_rows] == ["missing.png", "bad.png"]
    assert loaded.error_rows[0]["errors"]["input"].startswith("FileNotFoundError")
    assert "input" in loaded.error_rows[1]["errors"]
