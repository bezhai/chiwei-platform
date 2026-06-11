"""prepare_for_run 接线 Dynamic Config 的 lane provider（双入口共用路径）。

agent-service 双入口（FastAPI ``app.main`` lifespan / worker
``app.workers.runtime_entry``）都走 ``prepare_for_run``。Dynamic Config 是
per-lane 解析的，必须在这条共用路径上接上进程级部署泳道
（``current_deployment_lane``）——只挂 main.py 的话 worker 进程会永远读
prod 配置（dual-entry footgun）。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from inner_shared.dynamic_config import DynamicConfig, dynamic_config

from app.runtime.bootstrap import prepare_for_run


def test_set_lane_provider_drives_lane_resolution():
    """DynamicConfig.set_lane_provider：provider 返回泳道则用之，None 退 prod。"""
    cfg = DynamicConfig(paas_engine_url="http://example.invalid")
    assert cfg._get_lane() == "prod"

    cfg.set_lane_provider(lambda: "coe-x")
    assert cfg._get_lane() == "coe-x"

    cfg.set_lane_provider(lambda: None)
    assert cfg._get_lane() == "prod"


@pytest.mark.asyncio
async def test_prepare_for_run_wires_dynamic_config_lane_provider(monkeypatch):
    """prepare_for_run 后，全局 dynamic_config 按进程级部署泳道解析。

    LANE=coe-* 时读 coe 泳道配置；LANE 未设（prod 部署）退回 "prod"。
    """
    with patch("app.runtime.bootstrap.load_dataflow_graph", MagicMock()), \
         patch(
             "app.runtime.delayed_trigger.register_runtime_trigger_wire",
             MagicMock(),
         ):
        await prepare_for_run("agent-service")

    monkeypatch.setenv("LANE", "coe-feedwl")
    assert dynamic_config._get_lane() == "coe-feedwl"

    monkeypatch.delenv("LANE", raising=False)
    assert dynamic_config._get_lane() == "prod"
