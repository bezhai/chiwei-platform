"""wd14/eva02 tagger 打标器：onnx，接收 PIL 对象。

复用 wd14_infer.py 的纯函数（normalize_scores/read_tags/infer_size/build_payload），只 preprocess_pil
是 PIL 版（现有 preprocess_image 耦合 path、无法直接复用）——这份预处理短期与 CLI 重复，列 MVP 后统一。
同一个类用不同 model_repo 即得 wd14 / eva02 两个打标器。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from app.wd14_infer import build_payload, infer_size, normalize_scores, read_tags


def preprocess_pil(image: Image.Image, *, size: int, nchw: bool) -> np.ndarray:
    image = ImageOps.exif_transpose(image)
    if image.mode == "RGBA":
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        background.alpha_composite(image)
        image = background.convert("RGB")
    else:
        image = image.convert("RGB")
    width, height = image.size
    square = max(width, height)
    canvas = Image.new("RGB", (square, square), (255, 255, 255))
    canvas.paste(image, ((square - width) // 2, (square - height) // 2))
    canvas = canvas.resize((size, size), Image.Resampling.LANCZOS)
    array = np.asarray(canvas, dtype=np.float32)[:, :, ::-1]
    if nchw:
        array = np.transpose(array, (2, 0, 1))
    return np.expand_dims(array, axis=0)


class Wd14Tagger:
    def __init__(
        self,
        name: str,
        model_repo: str = "SmilingWolf/wd-vit-tagger-v3",
        model_dir: Path | None = None,
        tag_threshold: float = 0.35,
        character_threshold: float = 0.35,
        max_tags: int = 80,
    ) -> None:
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download

        self.name = name
        self.model_repo = model_repo
        self.tag_threshold = tag_threshold
        self.character_threshold = character_threshold
        self.max_tags = max_tags
        if model_dir is not None:
            model_path = str(model_dir / "model.onnx")
            tags_path = model_dir / "selected_tags.csv"
        else:
            model_path = hf_hub_download(model_repo, "model.onnx")
            tags_path = Path(hf_hub_download(model_repo, "selected_tags.csv"))
        self.tags = read_tags(tags_path)
        self.session = ort.InferenceSession(
            model_path, providers=["CUDAExecutionProvider", "CPUExecutionProvider"]
        )
        meta = self.session.get_inputs()[0]
        self.input_name = meta.name
        self.size = infer_size(meta.shape)
        self.nchw = len(meta.shape) == 4 and meta.shape[1] == 3

    def tag(self, image: Image.Image) -> dict[str, Any]:
        try:
            tensor = preprocess_pil(image, size=self.size, nchw=self.nchw)
            output = self.session.run(None, {self.input_name: tensor})[0][0].astype(np.float32)
            scores = normalize_scores(output)
            payload = build_payload(
                {},
                tags=self.tags,
                scores=scores,
                model=self.model_repo,
                tag_threshold=self.tag_threshold,
                character_threshold=self.character_threshold,
                max_tags=self.max_tags,
            )
            return {"tags": payload["tags"], "rating": payload["rating"]}
        except Exception as exc:
            return {"tags": None, "rating": None, "error": f"{type(exc).__name__}: {exc}"}
