"""Re-export real-pg fixtures so ``app.living`` tests can use ``test_db``.

Same reason as ``tests/domain/conftest.py``: the session-scoped
``test_db_dsn`` / function-scoped ``test_db`` fixtures live in
``tests/runtime/conftest.py`` and pytest only auto-discovers conftest
fixtures along the rootdir→test-file path.

**``living_db`` 在 docker 不可用时红，不 skip。** ``test_db_dsn`` 那边统一
``pytest.skip("docker unavailable")``——对大多数套件是对的，但 ``app.living``
的读写契约（提交序、旁听裁剪、到期消费）**全部**只在真 pg 上才有意义：静默跳过
之后整个套件绿着过去，等于没有门禁。所以这里把"跳过"翻译成"炸"，且只影响
``tests/living``——别人的 conftest 一个字不改。
"""
from __future__ import annotations

import pytest

from tests.runtime.conftest import test_db, test_db_dsn  # noqa: F401


def _docker_unavailable_reason() -> str | None:
    """docker 不可用时给出原因；可用返回 ``None``。"""
    try:
        import docker as _docker
    except Exception as exc:  # pragma: no cover - 依赖缺失路径
        return f"docker SDK 装不上：{exc!r}"
    try:
        _docker.from_env().ping()
    except Exception as exc:
        return f"连不上 docker daemon：{exc!r}"
    return None


@pytest.fixture(scope="session")
def real_pg_required() -> None:
    """没有真 pg 就让 ``tests/living`` 红，而不是绿着跳过。"""
    reason = _docker_unavailable_reason()
    if reason is not None:
        pytest.fail(
            "tests/living 需要真实 Postgres（testcontainers），但 "
            f"{reason}\n"
            "这些用例验的是提交序、按位置旁听、到期消费这些只在真库上成立的"
            "契约，静默 skip 会让整个套件绿着过去 —— 等于没有门禁。\n"
            "起 docker 后重跑；确实要在无 docker 的机器上只跑纯函数部分，"
            "显式点名：pytest tests/living/test_place.py tests/living/test_serial.py",
            pytrace=False,
        )


# bot_config / common_bot_presence 由 channel-server 管理，agent-service 侧没有
# SQLAlchemy 模型（只读裸表），所以要手建。只建查询真的用到的列。
# ``bot_config.common_user_id`` 是**群里点名判定**的全部依据：mention item 的 meta
# 里存的就是被点到那个 bot 的 common_user_id（见 channel-server 的 mention-utils）。
_BOT_CONFIG_DDL = (
    "CREATE TABLE bot_config ("
    "  bot_name VARCHAR(50) PRIMARY KEY,"
    "  persona_id VARCHAR(50),"
    "  common_user_id UUID,"
    "  is_active BOOLEAN NOT NULL DEFAULT TRUE"
    ")"
)
_BOT_PRESENCE_DDL = (
    "CREATE TABLE common_bot_presence ("
    "  common_conversation_id UUID NOT NULL,"
    "  bot_name VARCHAR(50) NOT NULL,"
    "  is_active BOOLEAN NOT NULL DEFAULT TRUE,"
    "  PRIMARY KEY (common_conversation_id, bot_name)"
    ")"
)


@pytest.fixture
async def living_db(real_pg_required, test_db):  # noqa: F811 — 形参名就是 fixture 名
    """建齐 ``app.living`` 读写要碰的所有表。

    除了 living 自己那几张，还包括**手机那条路要读的外部表**（``common_user`` /
    ``common_conversation`` / ``common_message`` + channel-server 那两张裸表）。
    它们建在这里而不是各个用例文件里，是因为**每一缝都会读手机的信封**——任何跑
    ``run_moment`` 的用例都要用到，各建各的迟早会出现"这个文件建了那个没建"。

    线上对应的是 T5 的种子：``ensure_business_schema()`` 只建 SQLAlchemy Base 那批，
    channel-server 的 TypeORM 表要人工建。缺了不会静默——信封那步直接炸，跟这里一样。
    """
    from sqlalchemy import text

    from app.data.models import (
        Base,
        CommonConversation,
        CommonMessage,
        CommonUser,
    )
    from app.living.mouth import SpokenOutbound
    from app.living.phone import PhoneRead
    from app.living.records import Happening, Upcoming, Whereabouts
    from tests.runtime.conftest import migrate

    for cls in (Happening, Whereabouts, Upcoming, PhoneRead, SpokenOutbound):
        await migrate(cls, test_db)
    tables = [
        CommonUser.__table__,
        CommonConversation.__table__,
        CommonMessage.__table__,
    ]
    async with test_db.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=tables)
        )
        await conn.execute(text(_BOT_CONFIG_DDL))
        await conn.execute(text(_BOT_PRESENCE_DDL))
    yield test_db


@pytest.fixture
def in_a_moment():
    """把一个工具放进"她的某一缝"里跑 —— 绑上工具体要读的那四样 ambient 事实。

    工具体一律走 :func:`app.living.scope.moment_scope`，没绑 context 直接
    ``LookupError``。用例只想验一个工具（看手机、发消息）时不必真跑一整缝，但
    **必须走真的 context 绑定**，不然 lane 隔离、派生 id、缝的身份全是假的。
    """
    import datetime as dt
    from contextlib import asynccontextmanager

    from app.agent.context import AgentContext
    from app.agent.runtime_context import agent_context
    from app.living.scope import (
        FEATURE_GLANCES,
        FEATURE_MOMENT,
        FEATURE_PERSONA,
        FEATURE_RECORDED,
        FEATURE_SWITCHES,
    )
    from app.living.world import FEATURE_LANE, FEATURE_NOW

    cst = dt.timezone(dt.timedelta(hours=8))

    @asynccontextmanager
    async def bind(
        persona_id: str,
        *,
        lane: str = "coe-living",
        now: dt.datetime | None = None,
        moment_id: str = "2026-07-25T21:30+08:00",
        finishes: bool = True,
    ):
        """``finishes=False`` = 这一缝崩在半路，收尾那一步没跑到。

        手机游标是**跟着一缝落地的**（见 :func:`app.living.phone.commit_glances`），
        所以"这一缝跑完没有"是它的输入，不能不给。
        """
        at = now or dt.datetime(2026, 7, 25, 21, 30, tzinfo=cst)
        ctx = AgentContext(
            persona_id=persona_id,
            features={
                FEATURE_LANE: lane,
                FEATURE_NOW: at.isoformat(),
                FEATURE_PERSONA: persona_id,
                FEATURE_MOMENT: moment_id,
                FEATURE_SWITCHES: [],
                FEATURE_RECORDED: [],
                FEATURE_GLANCES: [],
            },
        )
        with agent_context(ctx):
            yield ctx
        if finishes:
            from app.data.session import get_session
            from app.living.phone import commit_glances

            async with get_session() as s:
                await commit_glances(
                    glances=ctx.features[FEATURE_GLANCES], session=s
                )

    return bind
