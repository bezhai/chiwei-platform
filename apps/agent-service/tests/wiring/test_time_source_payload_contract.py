"""时间源 Data 形态契约测试 —— 抓"编译期溜过、生产源循环才炸"那类 bug.

框架硬约定（runtime/engine.py ``_build_payload``）：cron / interval 源每次 tick
**只用 ``w.data_type(ts=<iso>)`` 构造** payload —— 时间源的 Data 必须是带
``ts: str`` 字段的单字段 tick（正例见 ``app/domain/life_dataflow.py`` 的
``MinuteTick(ts: Annotated[str, Key])``）。

``compile_graph()`` 不跑源循环，所以一条时间源的 Data 形态不对（缺 ts / 有其他
必填字段）编译期检测不到——49 wires 照样编译过、集成测试照样过，但生产源循环
第一次 tick 就 ``_build_payload`` raise → ``_record_source_error`` → watchdog
``os._exit(1)`` → Pod 被杀重启 → 该源驱动的整条链路在生产里永远起不来。

这个文件对生产图里**每一条带 cron/interval 源的 wire** 断言其 ``data_type``
满足这条契约（直接复用 ``Runtime._build_payload`` 真实构造，不另起炉灶）。它能抓
住这一整类 bug，不只这一次 WorldTick。
"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest


def _rebuild_production_graph():
    """从头重建生产 wiring 再编译，避免依赖兄弟测试遗留的 WIRING_REGISTRY 状态.

    兄弟 wiring 测试会 ``clear_wiring()`` + reload 单个子模块，留下不完整的
    registry。这里清空后重新加载 ``app.wiring`` 全部子模块，让每条 ``wire(...)``
    重新注册，得到与生产进程 bootstrap 同口径的完整图。
    """
    import app.wiring as wiring_pkg
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    # 先 reload 各子模块（@node / bind 注册），再 reload 包（触发 wire(...)）。
    for sub in (
        "admin",
        "agent_tool_events",
        "chat",
        "fetch_dataflow",
        "life_dataflow",
        "memory",
        "memory_triggers",
        "memory_vectorize",
        "safety",
    ):
        importlib.reload(importlib.import_module(f"app.wiring.{sub}"))
    importlib.reload(wiring_pkg)

    from app.runtime.graph import compile_graph

    return compile_graph()


def _time_source_wires(graph):
    """生产图里所有带 cron / interval 源的 wire（这些源走 _build_payload）。"""
    return [
        w
        for w in graph.wires
        if any(s.kind in ("cron", "interval") for s in w.sources)
    ]


# parametrize ID 只需要时间源 Data 的名字（稳定）；测试体自己重建图、自取 wire。
_TIME_SOURCE_NAMES = [
    w.data_type.__name__ for w in _time_source_wires(_rebuild_production_graph())
]


def test_production_graph_has_time_sources():
    """前置健全：生产图确实有时间源 wire，否则下面的契约断言会 vacuously pass。"""
    graph = _rebuild_production_graph()
    assert _time_source_wires(graph), "生产图没有任何 cron/interval 源 wire"


@pytest.mark.parametrize("wire_name", _TIME_SOURCE_NAMES)
def test_time_source_data_type_satisfies_single_field_ts_contract(wire_name):
    """每条时间源 wire 的 data_type 必须能被 _build_payload(ts=<iso>) 构造.

    复用真实 ``Runtime._build_payload`` —— 它就是生产源循环每 tick 调的那段。
    构造不出来（缺 ts / 有其他必填字段）它会 raise RuntimeError，等同于生产
    第一次 tick 杀 Pod。这里把那次"杀 Pod"提前到 CI。
    """
    from app.runtime.engine import Runtime

    graph = _rebuild_production_graph()
    wire = next(
        w for w in _time_source_wires(graph) if w.data_type.__name__ == wire_name
    )

    runtime = Runtime(migrate_schema_on_run=False)
    # 不 raise == 满足单字段 ts 约定（生产源循环能正常 tick）。
    payload = runtime._build_payload(wire, datetime.now(tz=UTC))
    assert payload is not None


def test_world_heartbeat_wire_payload_buildable():
    """复现 bug（TDD red）：world 心跳那条 interval 源的 data_type 必须能被
    _build_payload 构造。

    修前：心跳源接 ``WorldTick``（无 ts、且缺必填 ``lane`` Key）→ _build_payload
    raise → 生产源循环杀 Pod → world 永远起不来。这条测试在修前必 fail。
    修后：心跳源接单字段 ``WorldHeartbeatTick(ts=...)`` → 构造成功 → 转绿。
    """
    from app.runtime.engine import Runtime

    graph = _rebuild_production_graph()
    # world_tick 由心跳驱动 —— 找到那条 interval 源 wire（它的 data_type 是
    # 真正被时间源喂的 tick）。心跳现在经翻译节点 heartbeat_to_world_tick 接到
    # world_tick；这里只认 interval 源那条 wire 的 data_type 能否被构造。
    heartbeat_wires = [
        w
        for w in _time_source_wires(graph)
        if any(s.kind == "interval" for s in w.sources)
        and any(
            getattr(c, "__name__", "")
            in ("world_tick", "heartbeat_to_world_tick")
            for c in w.consumers
        )
    ]
    assert heartbeat_wires, "找不到驱动 world 心跳的 interval 源 wire"

    runtime = Runtime(migrate_schema_on_run=False)
    for w in heartbeat_wires:
        # 修前这里对 WorldTick raise；修后对 WorldHeartbeatTick 通过。
        payload = runtime._build_payload(w, datetime.now(tz=UTC))
        assert payload is not None
