"""通信机制的集成测试夹具：真 Postgres + 带 delayed-message 插件的真 RabbitMQ。

通信机制的契约（定时送达、按泳道隔离、死信位置、重试退避）全部落在 broker 的
x-delayed-message 交换机和队列参数上，替身证明不了任何一条，所以这里用平台自己的
RabbitMQ 镜像（``harbor.local:30002/inner-bot/rabbitmq``，插件已启用），而不是
``tests/runtime/conftest.py`` 里那个关掉延时的原版镜像。

docker 或镜像不可用时整组 skip，跟 ``tests/runtime`` 的口径一致。
"""
from __future__ import annotations

import dataclasses
import os
from collections.abc import AsyncGenerator

import pytest

from tests.runtime.conftest import test_db, test_db_dsn  # noqa: F401

BROKER_IMAGE = os.getenv(
    "MESSAGING_TEST_BROKER_IMAGE", "harbor.local:30002/inner-bot/rabbitmq:latest"
)
LANE = "coe-msg"


@pytest.fixture(scope="session")
def delayed_broker() -> object:
    """Session 级：起一个只绑 127.0.0.1 的 RabbitMQ（带延时插件），交回 (amqp_url, http_api)。"""
    pytest.importorskip("testcontainers.rabbitmq")
    try:
        import docker as _docker

        client = _docker.from_env()
        client.ping()
        client.images.get(BROKER_IMAGE)
    except Exception as exc:
        pytest.skip(f"broker image {BROKER_IMAGE} unavailable: {exc}")

    from testcontainers.rabbitmq import RabbitMqContainer

    rmq = RabbitMqContainer(BROKER_IMAGE)
    rmq.ports = {5672: ("127.0.0.1", None), 15672: ("127.0.0.1", None)}
    rmq.start()
    try:
        host = rmq.get_container_host_ip()
        amqp_port = rmq.get_exposed_port(5672)
        http_port = rmq.get_exposed_port(15672)
        yield (
            f"amqp://{rmq.username}:{rmq.password}@{host}:{amqp_port}/",
            (f"http://{host}:{http_port}", (rmq.username, rmq.password)),
        )
    finally:
        rmq.stop()


async def _reset_mq() -> None:
    from app.infra.rabbitmq import mq

    await mq.close()
    mq._connection = None  # type: ignore[attr-defined]
    mq._channel = None  # type: ignore[attr-defined]
    mq._exchange = None  # type: ignore[attr-defined]
    mq._declared_lane_queues = set()  # type: ignore[attr-defined]


@pytest.fixture
async def broker(delayed_broker, monkeypatch) -> AsyncGenerator[object, None]:
    """函数级：把模块级 ``mq`` 接到测试 broker 上，并清空 broker 上的全部队列。

    每个测试从一个没有任何队列的 broker 开始——"收件箱没开设"是靠队列不存在来表达的，
    上一个测试留下的队列会让这类断言失真。
    """
    import httpx

    from app.infra import config as config_mod
    from app.infra import rabbitmq as rabbitmq_mod
    from app.infra.rabbitmq import mq

    amqp_url, (http_api, auth) = delayed_broker
    monkeypatch.delenv("RABBITMQ_DISABLE_DELAYED", raising=False)
    monkeypatch.setenv("LANE", LANE)
    new_settings = dataclasses.replace(config_mod.settings, rabbitmq_url=amqp_url)
    monkeypatch.setattr(config_mod, "settings", new_settings)
    monkeypatch.setattr(rabbitmq_mod, "settings", new_settings)

    async with httpx.AsyncClient(base_url=http_api, auth=auth, timeout=10) as http:
        for q in (await http.get("/api/queues/%2F")).json():
            await http.delete(f"/api/queues/%2F/{q['name']}")

    await _reset_mq()
    await mq.connect()
    await mq.declare_topology()
    try:
        yield BrokerHandle(http_api, auth)
    finally:
        from app.messaging.lifecycle import stop_messaging

        await stop_messaging()
        await _reset_mq()


class BrokerHandle:
    """测试里直接看 broker 的那一面：队列在不在、里面有几条、参数是什么。"""

    def __init__(self, http_api: str, auth: tuple[str, str]) -> None:
        self._http_api = http_api
        self._auth = auth

    async def queue(self, name: str) -> dict | None:
        import httpx

        async with httpx.AsyncClient(
            base_url=self._http_api, auth=self._auth, timeout=10
        ) as http:
            r = await http.get(f"/api/queues/%2F/{name}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()

    async def depth(self, name: str) -> int:
        """队列里现在就绪的消息数；队列不存在是 -1。

        走 AMQP 的 passive declare 而不是 management API：后者的计数按统计周期刷新，
        读到的是几秒前的数。
        """
        import aiormq

        from app.infra.rabbitmq import mq

        channel = await mq._connection.channel()  # type: ignore[union-attr]
        try:
            queue = await channel.declare_queue(name, passive=True)
            return int(queue.declaration_result.message_count)
        except aiormq.exceptions.ChannelNotFoundEntity:
            return -1
        finally:
            if not channel.is_closed:
                await channel.close()


@pytest.fixture
async def messaging_db(test_db) -> AsyncGenerator[object, None]:  # noqa: F811
    """记录者、去重状态、死信重放审计三张表。

    记录者的表按 :class:`app.data.models.MessageRecord` 的声明建，跟 coe-* 泳道启动时
    ``ensure_business_schema()`` 建出来的是同一份。
    """
    from sqlalchemy import text

    from app.data.models import MessageRecord
    from app.runtime.dlq_audit import RUNTIME_DLQ_AUDIT_DDL
    from app.runtime.inflight import RUNTIME_INFLIGHT_DDL

    async with test_db.begin() as conn:
        for ddl in (*RUNTIME_INFLIGHT_DDL, *RUNTIME_DLQ_AUDIT_DDL):
            await conn.execute(text(ddl))
        await conn.run_sync(lambda sync: MessageRecord.__table__.create(sync))
    yield test_db
