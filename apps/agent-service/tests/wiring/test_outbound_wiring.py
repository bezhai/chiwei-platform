"""她说出去的两条边，各自声明在哪个模块里。

这两条是 living 引擎**唯一**的出 graph 出口：

  * ``ChatResponseSegment -> Sink.mq("chat_response")`` —— 嘴（``app.living.mouth``
    emit 它）。没有它她能想不能说。
  * ``Recall -> Sink.mq("recall")`` —— 撤回（``app.living.takeback`` emit 它）。

之所以要**按模块**验而不是只验"注册表里有这条边"：这两条边原先分别搭在旧 chat
主 pipeline 和旧 pre/post 安全链的 wiring 模块上，而那两个模块整体属于旧实现。
整文件删掉时，只验"注册表里有"的测试会因为别处碰巧还有一条同名边而假绿；验
"这个模块 reload 之后有"才能钉住它确实搬到了保留下来的模块里。

reload 手法同 ``test_admin_wiring.py``：conftest 的 autouse fixture 先清
WIRING_REGISTRY，再 ``importlib.reload`` 重跑模块体让 ``wire(...)`` 重新注册。
"""
from __future__ import annotations

import importlib


def _fresh_import(module_name: str):
    """只让这一个 wiring 模块的 wire(...) 进注册表。"""
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    module = importlib.import_module(module_name)
    clear_wiring()
    clear_bindings()
    importlib.reload(module)
    return module


def _has_mq_sink(data_type, queue: str) -> bool:
    from app.runtime.sink import SinkSpec
    from app.runtime.wire import WIRING_REGISTRY

    return any(
        any(
            isinstance(s, SinkSpec)
            and s.kind == "mq"
            and s.params.get("queue") == queue
            for s in w.sinks
        )
        for w in WIRING_REGISTRY
        if w.data_type is data_type
    )


def test_the_mouth_is_declared_in_the_living_wiring():
    """``ChatResponseSegment -> Sink.mq("chat_response")`` 声明在 ``app.wiring.living``。

    她开口走的就是这条边。它原先挂在旧 chat 的 wiring 模块上（那个模块整体是旧
    实现），所以必须跟着 living 走——否则删旧实现的那一刀会把嘴一起删掉。
    """
    _fresh_import("app.wiring.living")

    from app.domain.chat_dataflow import ChatResponseSegment

    assert _has_mq_sink(ChatResponseSegment, "chat_response"), (
        "app.wiring.living 里没有 ChatResponseSegment -> Sink.mq('chat_response')"
        " —— 她能想不能说了。"
    )


def test_the_takeback_is_declared_in_the_safety_wiring():
    """``Recall -> Sink.mq("recall")`` 声明在 ``app.wiring.safety``。"""
    _fresh_import("app.wiring.safety")

    from app.domain.safety import Recall

    assert _has_mq_sink(Recall, "recall"), (
        "app.wiring.safety 里没有 Recall -> Sink.mq('recall') —— 她收不回已经"
        "说出去的话了。"
    )


def test_both_outbound_edges_are_live_together_after_the_whole_package_loads():
    """整包 ``app.wiring`` 导入后，两条出站边同时在注册表里。

    上面两条按模块验"声明在哪儿"，这条验"合起来真的都在"——两条边分居两个模块，
    只验单个模块会漏掉"某个模块没被 ``app.wiring.__init__`` import 进来"。
    """
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    for name in ("app.wiring.living", "app.wiring.safety"):
        importlib.reload(importlib.import_module(name))

    from app.domain.chat_dataflow import ChatResponseSegment
    from app.domain.safety import Recall

    assert _has_mq_sink(ChatResponseSegment, "chat_response")
    assert _has_mq_sink(Recall, "recall")
