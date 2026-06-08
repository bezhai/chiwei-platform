"""danbooru rating 四档 → [0,1] 连续露度分（0=安全，1=最露）。

派生字段：从 wd14/eva02 tagger 的 rating 算，不引新模型。四档 general/sensitive/questionable/explicit
有序，当等距位置 0/⅓/⅔/1，按概率求期望即这张图的露度位置；多 tagger 同口径等权平均。
anime_rating 口径不同（r15 把「性感不露」算进 nsfw，实测 85.8% vs 真露 23%），不混进加权——单独留作第二信号。
"""
from __future__ import annotations

_RUNG = {"general": 0.0, "sensitive": 1 / 3, "questionable": 2 / 3, "explicit": 1.0}


def rating_to_nsfw_score(rating: dict | None) -> float | None:
    """单个 danbooru rating（四档概率）→ [0,1] 露度分，按等距档位求期望。无数据返回 None。"""
    if not rating:
        return None
    total = sum(rating.get(k, 0.0) for k in _RUNG)
    if total <= 0:
        return None
    return sum(pos * rating.get(k, 0.0) for k, pos in _RUNG.items()) / total


def combine_nsfw(scores: list[float | None]) -> float | None:
    """多 tagger 的露度分等权平均（跳过 None）。全 None 返回 None。"""
    vals = [s for s in scores if s is not None]
    return sum(vals) / len(vals) if vals else None
