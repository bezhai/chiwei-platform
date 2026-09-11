"""一段要发出去的话过不过得了这一关 —— 以及这一关自己坏了的时候看不看得出来。

**这个模块存在的全部理由是把"真的通过"和"没判成"分开。** 原来的实现把异常吞在
自己肚子里，两种情况返回同一个"通过"，于是"那段时间漏了多少条没检查就发出去了"
这个问题在日志里和库里都答不出来。谁 fail-open 谁就欠一笔账，账得记得下来。
"""
from __future__ import annotations

import asyncio

import pytest

from app.capabilities import banned_words, output_safety
from app.capabilities.output_safety import audit_output


class _FakeGuard:
    """判词那一步的替身：要么给一个结果，要么炸，要么永远不回来。"""

    def __init__(
        self,
        *,
        unsafe: bool = False,
        confidence: float = 0.0,
        boom: Exception | None = None,
        hang: bool = False,
    ) -> None:
        self.unsafe = unsafe
        self.confidence = confidence
        self.boom = boom
        self.hang = hang
        self.asked: list[str] = []

    async def extract(self, schema, *, messages, prompt_vars):
        self.asked.append(prompt_vars["response"])
        if self.hang:
            await asyncio.sleep(3600)
        if self.boom is not None:
            raise self.boom
        return schema(is_unsafe=self.unsafe, confidence=self.confidence)


@pytest.fixture
def guard(monkeypatch):
    fake = _FakeGuard()
    monkeypatch.setattr(output_safety, "build_guard", lambda: fake)
    return fake


@pytest.fixture
def word_list(monkeypatch):
    """禁用词那一步的替身。默认干净。"""

    state: dict = {"hit": None, "boom": None, "hang": False, "asked": []}

    async def fake_contains(text: str) -> str | None:
        state["asked"].append(text)
        if state["hang"]:
            await asyncio.sleep(3600)
        if state["boom"] is not None:
            raise state["boom"]
        return state["hit"]

    monkeypatch.setattr(banned_words, "contains", fake_contains)
    return state


# --------------------------------------------------------------------------
# 一 · 拦得住的那些
# --------------------------------------------------------------------------


async def test_a_banned_word_stops_it_without_asking_the_model(guard, word_list):
    word_list["hit"] = "某个词"

    verdict = await audit_output("这句话里有某个词")

    assert verdict.ok is False
    assert verdict.reason == "output_banned_word"
    assert verdict.detail == "某个词", "命中哪个词要带出来，否则事后查不出拦的是什么"
    assert guard.asked == [], (
        "禁用词已经判死了，还去问一次模型就是白花一次调用"
    )


async def test_the_model_calling_it_unsafe_stops_it(guard, word_list):
    guard.unsafe = True
    guard.confidence = 0.9

    verdict = await audit_output("随便一句话")

    assert verdict.ok is False
    assert verdict.reason == "output_unsafe"


async def test_a_hedged_call_is_not_enough_to_stop_it(guard, word_list):
    """模型说"可能不安全"但自己都不确定 —— 照发。

    0.7 这个数是从 prod 上那条已经跑了很久的判词沿用下来的，不是这次新拍的。
    """
    guard.unsafe = True
    guard.confidence = 0.5

    verdict = await audit_output("随便一句话")

    assert verdict.ok is True
    assert verdict.checked is True, "判成了就是判成了，只是结论是放行"


# --------------------------------------------------------------------------
# 二 · 这一关自己坏了 —— 照发，但欠的这一笔要记下来
# --------------------------------------------------------------------------


async def test_a_broken_guard_lets_the_words_through_and_says_it_did_not_check(
    guard, word_list
):
    guard.boom = RuntimeError("模型挂了")

    verdict = await audit_output("随便一句话")

    assert verdict.ok is True, (
        "这一关坏掉时挡下来，挡的不是一条消息是整条线 —— 她和三个姐妹一起哑掉"
    )
    assert verdict.checked is False, (
        "照发可以，但必须说清楚这条没判过 —— 否则漏了多少永远查不出来"
    )


async def test_a_guard_that_never_answers_lets_the_words_through(guard, word_list):
    guard.hang = True

    verdict = await audit_output("随便一句话", timeout_s=0.05)

    assert verdict.ok is True
    assert verdict.checked is False


async def test_a_word_list_that_never_answers_does_not_hold_her_either(
    guard, word_list
):
    """期限要包住**整段**检查，不是只包住问模型那一步。

    禁用词走的是 Redis，而那条连接自己没有 socket / connect 超时 —— 它挂住的时候，
    一个只套在模型调用上的期限一秒都拦不住，她照样卡在网络上。
    """
    word_list["hang"] = True

    verdict = await asyncio.wait_for(
        audit_output("随便一句话", timeout_s=0.05), timeout=2.0
    )

    assert verdict.ok is True
    assert verdict.checked is False
    assert guard.asked == [], "前一步已经超时了，不该还去问模型"


async def test_no_deadline_means_it_waits(guard, word_list):
    """不给期限就是不设期限 —— 事后审计那条链上本来就没有期限，不替它加。"""
    guard.unsafe = True
    guard.confidence = 0.9

    verdict = await audit_output("随便一句话")

    assert verdict.ok is False, "没有 timeout_s 时不该被任何期限打断"


async def test_a_broken_word_list_still_asks_the_model_but_owes_the_same_note(
    guard, word_list
):
    """禁用词那一步坏了，判词那一步照跑 —— 但这条仍然没被完整判过。"""
    word_list["boom"] = RuntimeError("redis 断了")

    verdict = await audit_output("随便一句话")

    assert guard.asked == ["随便一句话"], "一步坏了不该让另一步也不跑"
    assert verdict.ok is True
    assert verdict.checked is False


# --------------------------------------------------------------------------
# 三 · 干净的那些
# --------------------------------------------------------------------------


async def test_a_clean_line_comes_back_checked(guard, word_list):
    verdict = await audit_output("你去过那家抹茶店吗")

    assert verdict.ok is True
    assert verdict.checked is True
    assert verdict.reason is None
    assert verdict.detail is None


async def test_nothing_to_say_is_not_an_unchecked_line(guard, word_list):
    """空的东西没有内容可判，不算欠账 —— 别让它把"漏了多少"这个数冲淡。"""
    verdict = await audit_output("   ")

    assert verdict.ok is True
    assert verdict.checked is True
    assert guard.asked == []
    assert word_list["asked"] == []
