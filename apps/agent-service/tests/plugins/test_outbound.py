"""她说出去的两条边，由哪个插件登记。

这两条是 living 引擎**唯一**的出站出口（``ctx.outbound``，:mod:`app.infra.outbound`）：

  * ``ChatResponseSegment -> chat_response`` —— 嘴（``app.living.speaking.mouth`` publish 它）。没有它
    她能想不能说。
  * ``Recall -> recall`` —— 撤回（``app.living.speaking.takeback`` publish 它）。

之所以要**按插件**验而不是只验"注册表里有这条边"：这两条边原先分别搭在旧 chat
主 pipeline 和旧 pre/post 安全链的接线模块上，而那两个模块整体属于旧实现。
整块删掉时，只验"注册表里有"的测试会因为别处碰巧还有一条同名边而假绿；验
"living 插件登记了它"才能钉住它确实跟着她走。
"""
from __future__ import annotations

from app.domain.chat_dataflow import ChatResponseSegment
from app.domain.safety import Recall


def _publishes_to(data_type, queue: str) -> bool:
    """``publish`` 看得到这条出站边：出站登记里 ``data_type`` 发往 ``queue``。"""
    from app.infra import outbound

    return outbound.registered().get(data_type) == queue


def _outbound(host) -> dict[tuple[type, str], str]:
    """宿主上登记的出站边：(Data, 队列) → 登记它的插件。"""
    return {
        (r.detail["data_type"], r.detail["queue"]): r.plugin
        for r in host.registered()
        if r.kind == "outbound"
    }


async def test_both_outbound_edges_are_registered_by_the_speaking_plugin(app_host):
    """她开口和撤回走的就是这两条边，都由 speaking 插件登记，起来之后 publish 看得到。"""
    host = await app_host("agent-service")

    assert _outbound(host) == {
        (ChatResponseSegment, "chat_response"): "speaking",
        (Recall, "recall"): "speaking",
    }, "speaking 插件没登记她的嘴或撤回 —— 她能想不能说，或者收不回已经说出去的话。"
    assert _publishes_to(ChatResponseSegment, "chat_response")
    assert _publishes_to(Recall, "recall")


async def test_stopping_the_host_takes_both_edges_back(app_host):
    host = await app_host("agent-service")

    await host.stop()

    assert not _publishes_to(ChatResponseSegment, "chat_response")
    assert not _publishes_to(Recall, "recall")
