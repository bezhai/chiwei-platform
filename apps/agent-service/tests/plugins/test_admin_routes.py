"""``app/api/routes.py`` 只挂 ``/health``：运维路由都由插件宿主挂（通信机制的人工入口
``app.plugins.operator``、读书队列的一次性搬运），完整的路由集合和各自的检查由
``tests/apps/test_surface.py`` 按进程钉住。``/health`` 答什么由这里钉住。
"""
from __future__ import annotations

import importlib
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import router as health_router


def test_routes_py_only_health():
    """routes.py 不能再有 admin/api endpoint。"""
    import app.api.routes as r

    importlib.reload(r)
    paths = {route.path for route in r.router.routes}
    assert paths == {"/health"}, f"routes.py paths drift: {paths}"


@pytest.mark.parametrize(("git_sha", "version"), [("baseline-sha", "baseline-sha"), (None, "unknown")])
async def test_health_answers_ok_with_the_service_and_the_deployed_version(
    monkeypatch, git_sha, version
):
    """``/health`` 的完整回答：200、JSON、``status`` 是 ``ok``，``version`` 是部署时注入的
    ``GIT_SHA``（没注入时是 ``unknown``），``timestamp`` 每次都不一样，只要求它在、是 ISO 时间。

    ``service`` 是 routes.py 里写死的 ``agent-service``，是镜像名不是 App 名：``app.main`` 不管
    ``APP_NAME`` 是哪个 App 都挂同一个 router，所以 world 的 ``/health`` 答的也是它。
    """
    if git_sha is None:
        monkeypatch.delenv("GIT_SHA", raising=False)
    else:
        monkeypatch.setenv("GIT_SHA", git_sha)
    application = FastAPI()
    application.include_router(health_router)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://health.test"
    ) as client:
        answer = await client.get("/health")

    assert answer.status_code == 200
    assert answer.headers["content-type"] == "application/json"
    body = answer.json()
    datetime.fromisoformat(body.pop("timestamp"))
    assert body == {"status": "ok", "service": "agent-service", "version": version}
