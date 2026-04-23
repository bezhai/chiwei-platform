"""Runtime integration test fixtures.

提供 ``test_db`` —— 基于 testcontainers 启动真实 Postgres 容器的 fixture。

关键约束：
  - **网络隔离**：容器端口必须绑定到 ``127.0.0.1``，不得 ``0.0.0.0``。
  - **会话级容器**：每个 pytest 进程启动一个容器，结束时销毁。
  - **函数级 engine**：每个测试得到独立的 ``AsyncEngine``；测试结束后 drop 所有
    ``public`` 下的表，下一个测试从空库开始。
  - **monkeypatch session**：同时替换 ``app.data.session.engine`` 和
    ``app.data.session.async_session``，保证被测代码走测试容器而不是 prod DSN。
  - **Docker 不可用时 skip**：CI 环境可能没有 docker，统一 skip 而不是红。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest

from app.runtime.data import Data


async def migrate(cls: type[Data], test_db: object) -> None:
    """在当前测试 engine 上为 ``cls`` 建表。

    由 ``plan_migration(existing_schema={})`` 生成完整 DDL，逐条 execute。
    多个测试文件共享此 helper，避免重复定义。
    """
    from sqlalchemy import text

    from app.runtime.migrator import plan_migration

    plan = plan_migration([cls], existing_schema={})
    async with test_db.begin() as conn:
        for s in plan.stmts:
            await conn.execute(text(s.sql))


@pytest.fixture(scope="session")
def rabbitmq_url() -> object:
    """Session-scoped: 启动 127.0.0.1-only RabbitMQ 容器，返回 amqp:// URL。

    We only own the container lifecycle at session scope — the actual
    ``mq.connect()`` / ``declare_topology()`` is per-test in the ``rabbitmq``
    function fixture, because aio-pika robust connections bind to the loop
    they were created on and pytest-asyncio gives each test its own loop.
    """
    pytest.importorskip("testcontainers.rabbitmq")

    try:
        import docker as _docker

        _docker.from_env().ping()
    except Exception:
        pytest.skip("docker unavailable; skipping rabbitmq integration tests")

    from testcontainers.rabbitmq import RabbitMqContainer

    rmq = RabbitMqContainer("rabbitmq:3-management-alpine")
    # CRITICAL: bind to loopback only, never 0.0.0.0.
    rmq.ports = {5672: ("127.0.0.1", None)}

    rmq.start()
    try:
        container = rmq.get_wrapped_container()
        container.reload()
        ports_attr = container.attrs["NetworkSettings"]["Ports"]["5672/tcp"]
        bind_ip = ports_attr[0]["HostIp"]
        assert bind_ip == "127.0.0.1", (
            f"rabbitmq container must bind to 127.0.0.1, got {bind_ip!r}; "
            f"container attrs: {ports_attr}"
        )

        host = rmq.get_container_host_ip()
        port = rmq.get_exposed_port(rmq.port)
        url = f"amqp://{rmq.username}:{rmq.password}@{host}:{port}/"

        yield url
    finally:
        rmq.stop()


@pytest.fixture
async def rabbitmq(
    rabbitmq_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[str, None]:
    """Function-scoped: connect the module ``mq`` singleton to the test container.

    Re-connects on every test so the ``aio_pika`` robust connection is bound
    to the current pytest-asyncio event loop. Also repoints
    ``settings.rabbitmq_url`` at the container for the duration of the test.
    """
    import dataclasses

    from app.infra import config as config_mod
    from app.infra import rabbitmq as rabbitmq_mod
    from app.infra.rabbitmq import mq

    # Vanilla rabbitmq images don't ship with the delayed-message plugin.
    # Tests don't exercise publish delays anyway; fall back to topic.
    monkeypatch.setenv("RABBITMQ_DISABLE_DELAYED", "1")

    new_settings = dataclasses.replace(
        config_mod.settings, rabbitmq_url=rabbitmq_url
    )
    # Patch both the module and the ``settings`` name that rabbitmq.py
    # imported into its own namespace.
    monkeypatch.setattr(config_mod, "settings", new_settings)
    monkeypatch.setattr(rabbitmq_mod, "settings", new_settings)
    # Force reconnect: previous test's connection is on a now-closed loop.
    await mq.close()
    # Reset the private connection/channel refs so connect() actually reconnects.
    mq._connection = None  # type: ignore[attr-defined]
    mq._channel = None  # type: ignore[attr-defined]
    mq._exchange = None  # type: ignore[attr-defined]
    mq._declared_lane_queues = set()  # type: ignore[attr-defined]

    await mq.connect()
    await mq.declare_topology()

    try:
        yield rabbitmq_url
    finally:
        await mq.close()
        mq._connection = None  # type: ignore[attr-defined]
        mq._channel = None  # type: ignore[attr-defined]
        mq._exchange = None  # type: ignore[attr-defined]


@pytest.fixture(scope="session")
def test_db_dsn() -> object:
    """Session-scoped: 启动一个绑定到 127.0.0.1 的 Postgres 容器，返回 async DSN。"""
    pytest.importorskip("testcontainers.postgres")

    try:
        import docker as _docker

        _docker.from_env().ping()
    except Exception:
        pytest.skip("docker unavailable; skipping real-pg integration tests")

    from testcontainers.postgres import PostgresContainer

    # driver='asyncpg' 让 get_connection_url() 直接返回 postgresql+asyncpg://
    pg = PostgresContainer("postgres:16-alpine", driver="asyncpg")

    # 关键：绑定到 127.0.0.1，而不是默认的 0.0.0.0。
    # testcontainers 把 self.ports 直接传给 docker-py 的 run()，value 支持
    # (host_ip, host_port) 元组；host_port=None 让 docker 随机分配。
    pg.ports = {5432: ("127.0.0.1", None)}

    pg.start()
    try:
        # 校验绑定 IP 正确 —— 容器必须对外部不可达
        container = pg.get_wrapped_container()
        container.reload()
        ports_attr = container.attrs["NetworkSettings"]["Ports"]["5432/tcp"]
        bind_ip = ports_attr[0]["HostIp"]
        assert bind_ip == "127.0.0.1", (
            f"test_db container must bind to 127.0.0.1, got {bind_ip!r}; "
            f"container attrs: {ports_attr}"
        )

        yield pg.get_connection_url()
    finally:
        pg.stop()


@pytest.fixture
async def test_db(
    test_db_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[object, None]:
    """Function-scoped: 给 ``app.data.session`` 注入指向测试容器的 engine。

    yields the test ``AsyncEngine`` 供测试直接用。

    teardown 阶段会 drop ``public`` schema 下所有表，保证测试之间互不影响。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    import app.data.session as session_mod

    test_engine = create_async_engine(
        test_db_dsn,
        echo=False,
        pool_pre_ping=True,
        future=True,
    )
    test_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    monkeypatch.setattr(session_mod, "engine", test_engine)
    monkeypatch.setattr(session_mod, "async_session", test_factory)

    try:
        yield test_engine
    finally:
        # 清理 public schema，保证下一个测试从空库开始
        async with test_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    DO $$
                    DECLARE r RECORD;
                    BEGIN
                        FOR r IN
                            SELECT tablename FROM pg_tables WHERE schemaname = 'public'
                        LOOP
                            EXECUTE 'DROP TABLE IF EXISTS public.'
                                || quote_ident(r.tablename) || ' CASCADE';
                        END LOOP;
                    END $$;
                    """
                )
            )
        await test_engine.dispose()
