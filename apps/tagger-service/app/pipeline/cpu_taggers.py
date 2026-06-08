"""CPU 类打标器：anime_rating（imgutils onnx）+ pHash（imagehash）。

都接收 PIL.Image 对象、不碰磁盘路径——这是和现有批处理 CLI（读 local_path）的关键区别。
推理调用是 thin 封装（一行库调用），核心算法在 imgutils / imagehash 库里，不重复项目逻辑。
"""
from __future__ import annotations

from typing import Any

from PIL import Image


class PHashTagger:
    """感知哈希：phash（DCT，抗压缩缩放）+ dhash（梯度差，便宜互补）。纯 CPU。"""

    name = "phash"

    def tag(self, image: Image.Image) -> dict[str, Any]:
        try:
            import imagehash
            from PIL import ImageOps

            img = ImageOps.exif_transpose(image).convert("RGB")
            return {"phash": str(imagehash.phash(img)), "dhash": str(imagehash.dhash(img))}
        except Exception as exc:
            return {"phash": None, "dhash": None, "error": f"{type(exc).__name__}: {exc}"}


class AnimeRatingTagger:
    """二次元 NSFW 连续分：imgutils anime_rating（safe/r15/r18 概率）。nsfw_score = r15 + r18。CPU onnx。"""

    name = "anime_rating"

    def __init__(self, model_name: str = "mobilenetv3_v1_pruned_ls0.1") -> None:
        self.model_name = model_name

    def tag(self, image: Image.Image) -> dict[str, Any]:
        try:
            from imgutils.validate import anime_rating_score

            scores = anime_rating_score(image, model_name=self.model_name)
            r15, r18 = scores["r15"], scores["r18"]
            return {"safe": scores["safe"], "r15": r15, "r18": r18, "nsfw_score": r15 + r18}
        except Exception as exc:
            return {
                "safe": None,
                "r15": None,
                "r18": None,
                "nsfw_score": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
