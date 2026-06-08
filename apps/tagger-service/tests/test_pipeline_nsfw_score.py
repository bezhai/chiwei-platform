from __future__ import annotations

from pytest import approx

from app.pipeline.nsfw_score import combine_nsfw, rating_to_nsfw_score


def test_pure_explicit_is_one() -> None:
    assert rating_to_nsfw_score({"explicit": 1.0}) == 1.0


def test_pure_general_is_zero() -> None:
    assert rating_to_nsfw_score({"general": 1.0}) == 0.0


def test_middle_rungs_are_thirds() -> None:
    assert rating_to_nsfw_score({"sensitive": 1.0}) == 1 / 3
    assert rating_to_nsfw_score({"questionable": 1.0}) == 2 / 3


def test_mix_is_expected_position() -> None:
    # 一半 general 一半 explicit → 期望位置 0.5
    assert rating_to_nsfw_score({"general": 0.5, "explicit": 0.5}) == 0.5


def test_unnormalized_rating_is_normalized() -> None:
    # 概率和不为 1 时按 total 归一（防御 sigmoid 输出）
    assert rating_to_nsfw_score({"general": 1.0, "explicit": 1.0}) == 0.5


def test_empty_rating_returns_none() -> None:
    assert rating_to_nsfw_score({}) is None
    assert rating_to_nsfw_score(None) is None


def test_combine_averages_taggers() -> None:
    assert combine_nsfw([0.2, 0.4]) == approx(0.3)


def test_combine_skips_none() -> None:
    assert combine_nsfw([0.2, None]) == 0.2
    assert combine_nsfw([None, None]) is None
