"""persona 漂移 diff 飞书推送 — 落版之后告诉 bezhai「她变了哪一笔」(spec 决策 6).

出站契约（bezhai 2026-06-13 拍板）：不走 chat_response 队列，直接 POST 飞书
**自定义机器人 webhook**（告警专用 bot），URL 从环境变量
``PERSONA_REVIEW_WEBHOOK_URL`` 读。这些测试钉死传输层硬约束：

  * env 缺省 / 空白 → 不 POST（info 留痕、点名 env 变量名）、绝不抛——缺省态
    是「只落库不通知」；
  * env 在 → POST 一条飞书 incoming webhook 协议消息：
    ``{"msg_type": "text", "content": {"text": "<消息文本>"}}``，超时 10s；
  * 消息文本三态（与传输无关、codex 已评审过的组装逻辑）：unified diff 变化
    摘要在前 + 新版全文在后 / 原样重写明说「无变化」/ 版本指针 v{n-1}→v{n}；
  * fail-open 铁律：HTTP 非 2xx / 飞书返回错误码（body code != 0）/ POST 连接
    异常，任何一步炸都绝不向上抛（版本已落，推送只是事后通知），error 留痕
    可感知。

网络用 ``httpx.MockTransport`` 桩掉（项目既有姿势，零新依赖），POST 路径跑在
真实 httpx 语义下。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.life.persona_diff_push as pdp

_LANE = "coe-t3"
_PERSONA = "akao"
_OLD = "出厂身份正文：她是她。"
_NEW = "慢漂后的身份正文：经历长进了她是谁。"
_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token"

_ENV = "PERSONA_REVIEW_WEBHOOK_URL"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok_handler(_req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "msg": "success"})


@contextmanager
def _stub_webhook(handler: Callable[[httpx.Request], httpx.Response] = _ok_handler):
    """Patch httpx.AsyncClient（实现自建 client 的项目既有桩法）。

    记录每个出站 request 和 AsyncClient 构造 kwargs，handler 决定响应/异常。
    """
    seen: dict[str, object] = {"requests": [], "client_kwargs": []}
    real_client = httpx.AsyncClient

    def _recording(req: httpx.Request) -> httpx.Response:
        seen["requests"].append(req)  # type: ignore[union-attr]
        return handler(req)

    def _factory(*args, **kwargs):
        seen["client_kwargs"].append(dict(kwargs))  # type: ignore[union-attr]
        return real_client(transport=httpx.MockTransport(_recording), **kwargs)

    with patch(
        "app.capabilities.persona_notification.httpx.AsyncClient",
        side_effect=_factory,
    ):
        yield seen


def _raising_handler(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise exc

    return handler


async def _push(**overrides):
    kwargs = {
        "lane": _LANE,
        "persona_id": _PERSONA,
        "old_narrative": _OLD,
        "new_narrative": _NEW,
        "version": 2,
    }
    kwargs.update(overrides)
    await pdp.push_persona_diff(**kwargs)


def _posted_text(seen: dict) -> str:
    """唯一一条出站 POST 的飞书 text 载荷。"""
    (req,) = seen["requests"]
    return json.loads(req.content)["content"]["text"]


# ---------------------------------------------------------------------------
# env 缺省：不推、info 留痕、不抛
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_env_skips_push_with_info(monkeypatch, caplog):
    """env 未设置 → 不 POST、info 点名 env 变量名、绝不抛。"""
    monkeypatch.delenv(_ENV, raising=False)

    with _stub_webhook() as seen, caplog.at_level(logging.INFO):
        await _push()  # 不抛

    assert seen["requests"] == []
    assert any(
        _ENV in r.message and r.levelno == logging.INFO for r in caplog.records
    ), "缺省不推要 info 留痕（点名 env 变量名）"


@pytest.mark.asyncio
async def test_blank_env_skips_push(monkeypatch):
    """全空白 env = 缺省：不 POST。"""
    monkeypatch.setenv(_ENV, "   ")

    with _stub_webhook() as seen:
        await _push()

    assert seen["requests"] == []


# ---------------------------------------------------------------------------
# env 在：POST 一条飞书 incoming webhook 协议消息
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configured_push_posts_feishu_text_payload(monkeypatch):
    """协议形状钉死：POST 到 env 指定 URL，JSON body
    {"msg_type": "text", "content": {"text": ...}}，10s 超时。"""
    monkeypatch.setenv(_ENV, _URL)

    with _stub_webhook() as seen:
        await _push()

    (req,) = seen["requests"]
    assert req.method == "POST"
    assert str(req.url) == _URL
    body = json.loads(req.content)
    assert body["msg_type"] == "text"
    assert isinstance(body["content"]["text"], str) and body["content"]["text"]
    assert set(body) == {"msg_type", "content"}, "飞书 text 协议只有这两个键"
    assert any(
        kw.get("timeout") == 10.0 for kw in seen["client_kwargs"]
    ), "webhook POST 要带 10s 超时（同 alert-webhook 姿势）"


@pytest.mark.asyncio
async def test_configured_push_delegates_url_and_rendered_text(monkeypatch):
    """业务层只组装 diff 文本；webhook 传输与响应解释委托 typed capability。"""
    from app.capabilities.persona_notification import PersonaNotificationResult

    monkeypatch.setenv(_ENV, _URL)
    send = AsyncMock(return_value=PersonaNotificationResult.success(status_code=200))

    with patch.object(pdp, "send_persona_notification", send):
        await _push()

    send.assert_awaited_once()
    assert send.await_args.kwargs["url"] == _URL
    text = send.await_args.kwargs["text"]
    assert _NEW in text
    assert _PERSONA in text
    assert _LANE in text


@pytest.mark.asyncio
async def test_push_text_carries_new_full_and_version_pointer(monkeypatch):
    """消息文本：新版全文 + 版本对照提示（旧版省略成版本链指针，长度不失控）。"""
    monkeypatch.setenv(_ENV, _URL)

    with _stub_webhook() as seen:
        await _push()

    text = _posted_text(seen)
    assert _NEW in text, "新版全文必须在"
    assert "v2" in text
    assert "v1" in text, "上一版以版本链指针（v{n-1}）形式给出"
    assert _PERSONA in text
    assert _LANE in text, "bezhai 要能分辨这是哪个泳道的慢漂"


@pytest.mark.asyncio
async def test_push_text_shows_changed_lines_as_diff(monkeypatch):
    """新旧有差异 → 消息前部含 unified diff 变化块：被改的行（- 旧 / + 新）一眼
    可见，owner 不用对照两版全文自己找；新版全文跟在变化块之后。"""
    monkeypatch.setenv(_ENV, _URL)

    old = "她是赤尾。\n她在读高三。\n她喜欢画画。"
    new = "她是赤尾。\n她考完了试，正在等放榜。\n她喜欢画画。"
    with _stub_webhook() as seen:
        await _push(old_narrative=old, new_narrative=new)

    text = _posted_text(seen)
    assert "-她在读高三。" in text, "被改掉的旧行要在 diff 块里可见"
    assert "+她考完了试，正在等放榜。" in text, "改成的新行要在 diff 块里可见"
    assert new in text, "新版全文仍然完整在消息里"
    assert text.index("-她在读高三。") < text.index(new), (
        "变化摘要（diff 块）在前、新版全文在后"
    )


@pytest.mark.asyncio
async def test_push_text_no_change_says_so(monkeypatch):
    """原样重写（diff 为空）→ 明说「无变化、原样保留」，不留一个空 diff 段让
    owner 猜；新版全文仍在。"""
    monkeypatch.setenv(_ENV, _URL)

    same = "她是赤尾。\n她喜欢画画。"
    with _stub_webhook() as seen:
        await _push(old_narrative=same, new_narrative=same)

    text = _posted_text(seen)
    assert "无变化" in text
    assert "原样" in text
    assert same in text, "新版全文仍然完整在消息里"


@pytest.mark.asyncio
async def test_successful_push_no_error_log(monkeypatch, caplog):
    """飞书回 200 + code=0 → 成功路径不留 error 噪声。"""
    monkeypatch.setenv(_ENV, _URL)

    with _stub_webhook() as seen, caplog.at_level(logging.INFO):
        await _push()

    assert len(seen["requests"]) == 1
    assert not any(r.levelno >= logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# fail-open 铁律：任何一步炸都绝不向上抛（版本已落，推送只是事后通知）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_2xx_response_logs_error_not_raise(monkeypatch, caplog):
    """HTTP 非 2xx → 不抛、error 留痕。"""
    monkeypatch.setenv(_ENV, _URL)

    def _http_500(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    with _stub_webhook(_http_500) as seen, caplog.at_level(logging.ERROR):
        await _push()  # 不抛

    assert len(seen["requests"]) == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_feishu_error_code_logs_error_not_raise(monkeypatch, caplog):
    """HTTP 2xx 但飞书 body code != 0（如 token 错、字段不合法）→ 不抛、error
    留痕——飞书 webhook 的失败常以 200 + 错误码形式出现，不能只看状态码。"""
    monkeypatch.setenv(_ENV, _URL)

    def _feishu_reject(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 19001, "msg": "param invalid"})

    with _stub_webhook(_feishu_reject) as seen, caplog.at_level(logging.ERROR):
        await _push()  # 不抛

    assert len(seen["requests"]) == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_malformed_success_body_logs_error_not_raise(monkeypatch, caplog):
    """HTTP 2xx 但 body 不是 JSON → capability 解析失败，业务仍 fail-open。"""
    monkeypatch.setenv(_ENV, _URL)

    def _malformed(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    with _stub_webhook(_malformed) as seen, caplog.at_level(logging.ERROR):
        await _push()  # 不抛

    assert len(seen["requests"]) == 1
    assert any(r.levelno == logging.ERROR for r in caplog.records)


@pytest.mark.asyncio
async def test_post_exception_does_not_raise(monkeypatch, caplog):
    """POST 连接层炸（DNS / 拒连 / 超时）→ 不抛、error 留痕。"""
    monkeypatch.setenv(_ENV, _URL)

    with (
        _stub_webhook(_raising_handler(httpx.ConnectError("dns down"))),
        caplog.at_level(logging.ERROR),
    ):
        await _push()  # 不抛

    assert any(r.levelno == logging.ERROR for r in caplog.records)
