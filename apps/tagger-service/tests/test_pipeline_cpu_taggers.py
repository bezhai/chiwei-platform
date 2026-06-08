from __future__ import annotations

from PIL import Image

from app.pipeline.cpu_taggers import AnimeRatingTagger, PHashTagger


def _img(color: tuple[int, int, int] = (128, 100, 200)) -> Image.Image:
    return Image.new("RGB", (64, 64), color)


# 打标器统一契约：接收 PIL 图对象（不是路径）、有 .name、坏输入不 crash 而是回 error。

def test_phash_tagger_returns_hex_hashes() -> None:
    r = PHashTagger().tag(_img())
    assert isinstance(r["phash"], str) and len(r["phash"]) >= 8
    assert isinstance(r["dhash"], str) and len(r["dhash"]) >= 8
    assert "error" not in r


def test_phash_tagger_same_image_same_hash() -> None:
    t = PHashTagger()
    assert t.tag(_img())["phash"] == t.tag(_img())["phash"]


def test_phash_tagger_name() -> None:
    assert PHashTagger().name == "phash"


def test_phash_tagger_bad_input_returns_error() -> None:
    r = PHashTagger().tag(None)
    assert "error" in r
    assert r["phash"] is None


def test_anime_rating_tagger_returns_scores_summing_to_one() -> None:
    r = AnimeRatingTagger().tag(_img())
    assert {"safe", "r15", "r18", "nsfw_score"} <= set(r)
    assert abs((r["safe"] + r["r15"] + r["r18"]) - 1.0) < 0.05
    assert "error" not in r


def test_anime_rating_nsfw_score_is_r15_plus_r18() -> None:
    r = AnimeRatingTagger().tag(_img())
    assert abs(r["nsfw_score"] - (r["r15"] + r["r18"])) < 1e-9


def test_anime_rating_tagger_name() -> None:
    assert AnimeRatingTagger().name == "anime_rating"


def test_anime_rating_bad_input_returns_error() -> None:
    r = AnimeRatingTagger().tag(None)
    assert "error" in r
    assert r["nsfw_score"] is None
