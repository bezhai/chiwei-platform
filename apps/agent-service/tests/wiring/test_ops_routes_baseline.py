"""改前那五条运维口和 ``/health`` 的完整回答，钉成字面量。

这一份要回答的是一个很窄的问题：**给那四条文档树端点装门的那次改动，有没有顺手改掉
别的路由的行为。** 那次改动动的是所有自动注册路由共用的那一段——``Source.http`` 多了
两个开关、``_bind_one`` 的方法分发从四路 if/elif 收成一张表、每条路由多带一个
``dependencies=`` 参数，而参数校验失败那条 detail 现在要过一遍"回答外壳"。这几样
里的任何一样写歪了，波及的都不止那四条。

**期望值是从改前的实现上捕获的，不是从改后的实现上写下来的。** 做法：把
``app/runtime/source.py``、``app/runtime/http_source.py``、``app/wiring/admin.py``
临时还原到 HEAD，用下面这些**有效请求**打一遍，把整份回答（状态码、content-type、
响应体逐字）抄成下面的字面量，再把实现还原回来。

这跟"带不带凭据都答得一样"不是同一条：那条比的是改后的实现上三次调用彼此相同，证明
得了门没扩大到它们身上，证明不了行为跟改前逐字相同。而且那条比的是 422——参数校验
失败，请求根本没进到端点自己的活儿里。这一份用的是有效请求，走完整条路：路由 →
反序列化 → 节点 → 响应序列化。

下游（搜索、DLQ 那几个 impl）是替身，因为这一份验的不是它们，是**通到它们的那条路**：
替身让回答变成确定的，否则每次跑都不一样，钉不住任何东西。替身在改前改后是同一个。
"""
from __future__ import annotations

import dataclasses
import importlib
import json

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import router as health_router
from app.infra import config
from app.runtime.http_source import register_http_sources

BASE = "http://ops-routes.test"

# 打过去的那几个请求。标签、路径、方法、body 全是字面量。
#
# 主体是**有效请求**（走完整条路：路由 → 反序列化 → 节点 → 响应序列化），外加**一条
# 参数不全的**：框架挡回去的那种回答同样是这几条路由的行为，而它走的恰恰是这次改动
# 碰过的那一段（detail 要过一遍"回答外壳"）。少了它，一个"给所有路由的拒绝都加上
# 执行泳道"的改动能从这一份底下溜过去——有效请求根本不产生拒绝。
CALLS = (
    ("POST /admin/search", "POST", "/admin/search", {"queries": ["世界底子"], "num": 1}),
    (
        "POST /admin/dlq/inspect",
        "POST",
        "/admin/dlq/inspect",
        {"request_id": "r-1", "queue": "q-1", "limit": 3, "queue_kind": "dlq"},
    ),
    (
        "POST /admin/dlq/clear-idempotent",
        "POST",
        "/admin/dlq/clear-idempotent",
        {
            "request_id": "r-2",
            "by": "someone",
            "trace_id": "t-1",
            "edge_id": "e-1",
            "idempotent_key": "k-1",
        },
    ),
    (
        "POST /admin/dlq/dry-run",
        "POST",
        "/admin/dlq/dry-run",
        {"request_id": "r-3", "queue": "q-3", "limit": 2, "queue_kind": "dlq"},
    ),
    (
        "POST /admin/dlq/requeue",
        "POST",
        "/admin/dlq/requeue",
        {
            "request_id": "r-4",
            "queue": "q-4",
            "queue_kind": "dlq",
            "limit": 2,
            "clear_idempotent": True,
        },
    ),
    (
        "POST /admin/dlq/inspect（参数不全）",
        "POST",
        "/admin/dlq/inspect",
        {"request_id": "r-5"},
    ),
    ("GET /health", "GET", "/health", None),
)

# 改前那一版给出的完整回答。**从 HEAD 的实现上捕获**，不是照着改后的实现写的：
# 把 source.py / http_source.py / wiring/admin.py 还原到 HEAD（那时 http_auth.py 还
# 不存在），跑上面那几个请求，把输出抄下来。
#
# ``/admin/search`` 那条的 500 不是这次改动弄坏的：它 import
# ``app.agent.tools.search._you_search``，而那个函数在 ``app.capabilities.web_search``
# 里——这条 import 在改前就是坏的，所以这个端点今天答什么都是 500。钉住它是为了说清
# "改前也是这样"；哪天有人修好它，这条会红，那时是**故意**改期望值。
BEFORE_THE_DOOR: dict = {
    "POST /admin/search": {
        "status": 500,
        "content_type": "text/plain; charset=utf-8",
        "text": "Internal Server Error",
    },
    "POST /admin/dlq/inspect": {
        "status": 200,
        "content_type": "application/json",
        "text": '{"request_id":"r-1","rows":[{"queue":"q-1","limit":3,"queue_kind":"dlq"}]}',
    },
    "POST /admin/dlq/clear-idempotent": {
        "status": 200,
        "content_type": "application/json",
        "text": (
            '{"request_id":"r-2","deleted":2,"skipped_succeeded":1,"error":null,'
            '"edge_id":"e-1","idempotent_key":"k-1","status_code":200}'
        ),
    },
    "POST /admin/dlq/dry-run": {
        "status": 200,
        "content_type": "application/json",
        "text": '{"request_id":"r-3","plan":[{"queue":"q-3","limit":2}]}',
    },
    "POST /admin/dlq/requeue": {
        "status": 200,
        "content_type": "application/json",
        "text": (
            '{"request_id":"r-4","requeued":3,"publish_failed":0,'
            '"zombie_acked":1,"status_code":200}'
        ),
    },
    # 参数不全那一条：detail 是**一句话**，不是带执行泳道的那种壳。这几条运维口不自报
    # 落点，给它们加上就是改了它们的回答。
    "POST /admin/dlq/inspect（参数不全）": {
        "status": 422,
        "content_type": "application/json",
        "text": (
            '{"detail":"1 validation error for DlqInspectRequest\\nqueue\\n  '
            "Field required [type=missing, input_value={'request_id': 'r-5'}, "
            'input_type=dict]\\n    For further information visit '
            'https://errors.pydantic.dev/2.12/v/missing"}'
        ),
    },
    "GET /health": {
        "status": 200,
        "content_type": "application/json",
        "json_without_timestamp": {
            "status": "ok",
            "service": "agent-service",
            "version": "baseline-sha",
        },
    },
}


@pytest.fixture
def downstream(monkeypatch):
    """把搜索和 DLQ 那几个 impl 换成替身，让回答变成确定的。

    ``/health`` 里的 ``version`` 读的是 ``GIT_SHA``，也一起钉住；``timestamp`` 钉不住
    （它本来就每次都不一样），下面单独摘出去。
    """
    from app.nodes import dlq_admin

    # 搜索那条明确按"没配搜索后端"来跑：它自己的分支会答 503，而那是这个端点在这个
    # 进程里的真实回答。不去替身化再往下走，是因为再往下那一步现在根本走不通——
    # ``app/nodes/admin.py`` 从 ``app.agent.tools.search`` import ``_you_search``，
    # 而那个函数在 ``app.capabilities.web_search`` 里，这条 import 是坏的（跟本次改动
    # 无关，先不动它）。503 这条分支是它今天唯一走得完的一条。
    monkeypatch.setattr(
        config,
        "settings",
        dataclasses.replace(config.settings, you_search_host=None),
    )

    async def inspected(*, queue, limit=20, queue_kind="dlq"):
        return [{"queue": queue, "limit": limit, "queue_kind": queue_kind}]

    async def cleared(body, *, operator):
        return {
            "deleted": 2,
            "skipped_succeeded": 1,
            "error": None,
            "edge_id": body["edge_id"],
            "idempotent_key": body["idempotent_key"],
            "status_code": 200,
        }

    async def planned(body):
        return {"plan": [{"queue": body["queue"], "limit": body["limit"]}]}

    async def requeued(body, *, operator):
        return {
            "requeued": 3,
            "publish_failed": 0,
            "zombie_acked": 1,
            "status_code": 200,
        }

    monkeypatch.setattr(dlq_admin, "dlq_inspect_impl", inspected)
    monkeypatch.setattr(dlq_admin, "dlq_clear_idempotent_impl", cleared)
    monkeypatch.setattr(dlq_admin, "dlq_dry_run_impl", planned)
    monkeypatch.setattr(dlq_admin, "dlq_requeue_impl", requeued)
    monkeypatch.setenv("GIT_SHA", "baseline-sha")


@pytest.fixture
def api(downstream) -> FastAPI:
    """跟 main.py 同一套：``/health`` 那个 router 加上自动注册出来的那几条。"""
    import app.wiring.admin as admin_wiring

    importlib.reload(admin_wiring)
    application = FastAPI()
    application.include_router(health_router)
    register_http_sources(application)
    return application


@pytest.fixture
async def ops(api):
    """``raise_app_exceptions=False``：节点抛出来的异常按真服务器那样落成 500。

    ``/admin/search`` 今天就是这条路（见上面那条注释里的坏 import）。让它在客户端
    抛出来的话，钉住的就不是"这个端点答什么"而是"测试怎么炸的"。
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api, raise_app_exceptions=False),
        base_url=BASE,
    ) as client:
        yield client


async def test_the_ops_routes_answer_exactly_what_they_answered_before(ops):
    """五条运维口 + ``/health``，逐字跟改前一样。"""
    got: dict = {}
    for label, method, path, body in CALLS:
        answer = await ops.request(method, path, json=body)
        seen = {
            "status": answer.status_code,
            "content_type": answer.headers.get("content-type"),
            "text": answer.text,
        }
        if path == "/health":
            # timestamp 每次都不一样，是这个端点本来的样子；其余逐字。
            seen = {
                "status": answer.status_code,
                "content_type": answer.headers.get("content-type"),
                "json_without_timestamp": {
                    k: v for k, v in answer.json().items() if k != "timestamp"
                },
            }
        got[label] = seen

    # 失败时把这次实际收到的整份回答打全：要判断是"改坏了"还是"这几条端点本身改了
    # 口径"，看的就是这份东西，而它正是当初捕获期望值的那份输出。
    assert got == BEFORE_THE_DOOR, json.dumps(got, ensure_ascii=False, indent=2)
