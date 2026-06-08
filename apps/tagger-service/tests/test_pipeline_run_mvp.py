from __future__ import annotations

from PIL import Image

from app.pipeline.run_mvp import load_items


def _make_img(path, color=(200, 100, 50)) -> None:
    Image.new("RGB", (8, 8), color).save(path)


def test_load_items_constructs_id_image_pairs(tmp_path) -> None:
    p1 = tmp_path / "a.jpg"
    p2 = tmp_path / "b.jpg"
    _make_img(p1)
    _make_img(p2)
    assets = [
        {"pixiv_addr": "a", "local_path": str(p1)},
        {"pixiv_addr": "b", "local_path": str(p2)},
    ]
    items = load_items(assets)
    assert [image_id for image_id, _ in items] == ["a", "b"]
    assert all(isinstance(image, Image.Image) for _, image in items)


def test_load_items_respects_limit(tmp_path) -> None:
    p1 = tmp_path / "a.jpg"
    p2 = tmp_path / "b.jpg"
    _make_img(p1)
    _make_img(p2)
    assets = [
        {"pixiv_addr": "a", "local_path": str(p1)},
        {"pixiv_addr": "b", "local_path": str(p2)},
    ]
    items = load_items(assets, limit=1)
    assert [image_id for image_id, _ in items] == ["a"]


def test_load_items_skips_missing_and_corrupt(tmp_path) -> None:
    good = tmp_path / "good.jpg"
    _make_img(good)
    corrupt = tmp_path / "corrupt.jpg"
    corrupt.write_bytes(b"not an image")
    assets = [
        {"pixiv_addr": "good", "local_path": str(good)},
        {"pixiv_addr": "missing", "local_path": str(tmp_path / "nope.jpg")},
        {"pixiv_addr": "corrupt", "local_path": str(corrupt)},
        {"pixiv_addr": "no_path"},
    ]
    items = load_items(assets)
    # 坏图/缺图/缺路径都跳过、不崩，只留好图
    assert [image_id for image_id, _ in items] == ["good"]
