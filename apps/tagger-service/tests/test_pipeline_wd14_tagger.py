from __future__ import annotations

from PIL import Image

from app.pipeline.wd14_tagger import Wd14Tagger, preprocess_pil


def test_preprocess_pil_nchw_shape() -> None:
    t = preprocess_pil(Image.new("RGB", (100, 50)), size=448, nchw=True)
    assert t.shape == (1, 3, 448, 448)


def test_preprocess_pil_nhwc_shape() -> None:
    t = preprocess_pil(Image.new("RGB", (100, 50)), size=448, nchw=False)
    assert t.shape == (1, 448, 448, 3)


def test_preprocess_pil_handles_rgba() -> None:
    # RGBA 要合成白底再转 RGB，不能崩
    t = preprocess_pil(Image.new("RGBA", (64, 64), (10, 20, 30, 128)), size=448, nchw=True)
    assert t.shape == (1, 3, 448, 448)


def test_preprocess_pil_pads_to_square() -> None:
    # 非正方形图 pad 成正方形后 resize，长宽比不被拉伸（输出始终 size×size）
    t = preprocess_pil(Image.new("RGB", (200, 50)), size=224, nchw=False)
    assert t.shape == (1, 224, 224, 3)


# 端到端真实 onnx（CPU），首次会下 wd-vit-tagger-v3 的 model.onnx + selected_tags.csv。
def test_wd14_tagger_tags_an_image() -> None:
    t = Wd14Tagger(name="wd14", model_repo="SmilingWolf/wd-vit-tagger-v3")
    r = t.tag(Image.new("RGB", (256, 256), (120, 130, 140)))
    assert isinstance(r["tags"], list)
    assert isinstance(r["rating"], dict) and "general" in r["rating"]
    assert "error" not in r


def test_wd14_tagger_name() -> None:
    assert Wd14Tagger(name="wd14", model_repo="SmilingWolf/wd-vit-tagger-v3").name == "wd14"


def test_wd14_tagger_bad_input_returns_error() -> None:
    t = Wd14Tagger(name="wd14", model_repo="SmilingWolf/wd-vit-tagger-v3")
    r = t.tag(None)
    assert "error" in r
    assert r["tags"] is None
