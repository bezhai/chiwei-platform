"""Re-export real-pg fixtures so ``app.living`` tests can use ``test_db``.

Same reason as ``tests/domain/conftest.py``: the session-scoped
``test_db_dsn`` / function-scoped ``test_db`` fixtures live in
``tests/runtime/conftest.py`` and pytest only auto-discovers conftest
fixtures along the rootdir→test-file path.

**``living_db`` 在 docker 不可用时红，不 skip。** ``app.living`` 的读写契约（提交序、收件
去重、到期消费）**全部**只在真 pg 上才有意义，所以它先要 ``real_pg_required``
（``tests/conftest.py``），把 ``test_db_dsn`` 那边的"跳过"翻译成"炸"。
"""
from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from tests.runtime.conftest import test_db, test_db_dsn  # noqa: F401

if TYPE_CHECKING:
    from app.agent.round import AssembledProfile
    from app.agent.tooling import Tool
    from app.host import Turns
    from app.living.moment import LifeMoment

# 三姐妹在世界里的名字（线上取自人设表的显示名）。
RESIDENT_NAMES = {"akao": "赤尾", "ayana": "绫奈", "chinagi": "千凪"}


@pytest.fixture(autouse=True)
def residents_named(monkeypatch):
    """三姐妹的名字已经读好了，跟线上一样。

    线上进程开始接收消息时就读好这份对照（:func:`app.living.received.open_inboxes`），
    在任何一条钟开始拍之前；之后她每一轮发消息查地址、按名字认出她对谁说话，用的都是这一份。
    验"还没读"和"读的时候出错"的用例自己把它清掉再读。
    """
    from app.living import participants as participants_mod

    monkeypatch.setattr(
        participants_mod, "_known", participants_mod.Residents(dict(RESIDENT_NAMES))
    )


@dataclass(frozen=True)
class Sent:
    """她发出去的一条：交给通信机制时的样子。"""

    sender: str
    recipient: str
    body: str
    message_id: str
    # 消息上的时间（通信机制里的 ``time``）；``None`` 是没给，通信机制会填发出那一刻。
    time: datetime | None = None


class Post:
    """替身通信机制：记下她每一次发送；可以让发给某个名字的这一次失败、一直不返回，或者那个名字
    没开收件箱。

    失败的、挂住的那一次也记下：发送方眼里那是"试过了、没确认"，结果可能已经到了对方那里。
    挂住的那一次就是 broker 那头不应答的样子：不返回、也不报错，直到被取消。
    """

    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.failing: set[str] = set()
        self.hanging: set[str] = set()
        self.no_inbox: set[str] = set()

    async def send(self, *, sender, recipient, body, message_id=None, time=None):
        import asyncio

        from app.messaging.message import Delivery, SendFailed

        self.sent.append(Sent(sender, recipient, body, message_id, time))
        if recipient in self.hanging:
            await asyncio.Event().wait()
        if recipient in self.failing:
            raise SendFailed("broker 没有确认", message_id=message_id)
        if recipient in self.no_inbox:
            return Delivery(message_id, delivered=False, reason="对方没有开设收件箱")
        return Delivery(message_id, delivered=True)


@pytest.fixture
def post(monkeypatch) -> Post:
    """她往外发消息走这个替身，不碰 broker。"""
    from app.living import outgoing as outgoing_mod

    stand_in = Post()
    monkeypatch.setattr(outgoing_mod, "send", stand_in.send)
    return stand_in


# 她的一轮跑在哪条泳道上。一轮的泳道取自进程（宿主组装 ``resident`` 时读的 ``LANE``），线上它和
# 判出这一轮的那条泳道是同一个值；用例里的 ``LANE`` 都是这一条。
HER_LANE = "coe-living"


def her_plugins() -> list:
    """agent-service 清单里她那几个插件（guides 用的 skills，和她自己的全部插件），按清单的顺序，跟
    线上起的是同一份；运维接口和通信机制的人工入口不在里面。"""
    import importlib

    from app.manifests.agent_service import MANIFEST

    return [
        importlib.import_module(module).PLUGIN
        for module in MANIFEST.plugins
        if module not in ("app.plugins.ops", "app.plugins.operator")
    ]


@pytest.fixture
async def resident(app_host, monkeypatch, tmp_path):
    """她的一轮由什么组成：agent-service 的宿主起好（只跑 setup，不碰数据库、broker，不起钟），
    清单里 ``resident`` 那份按 living 登记的东西组好，跟线上交给她两个唤醒来源的是同一份
    （:mod:`app.plugins.living`）。

    进程的泳道设成 :data:`HER_LANE`。手边的说明从一个空目录读：skills 插件起来时会重读一遍，
    用例要哪几份说明自己装。
    """
    from app.host import Plugin

    monkeypatch.setenv("LANE", HER_LANE)
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    assembled = []
    her_round = Plugin(
        name="her-round", setup=lambda ctx: assembled.append(ctx.profile("resident"))
    )
    await app_host("agent-service", [*her_plugins(), her_round])
    return assembled[0]


def clock_at(moment: datetime) -> Callable[[], datetime]:
    """一只停在 ``moment`` 的钟：轮到她的时候读它，读几次都是这一刻。"""
    return lambda: moment


def her_queue(
    persona_id: str = "akao",
    line: str = "round",
    *,
    clock: Callable[[], datetime],
    hold_limit: timedelta | None = None,
) -> Turns:
    """她的一条队列，跟宿主给她登记的一样（:mod:`app.plugins.living`）：``round`` 是她的轮次，
    ``day-page``、``persona-review`` 是日记和每周回看。一条新的就是一个刚起来的进程。钟由用例给，
    轮到她的那一刻读它。"""
    from app.host import Turns

    return Turns(persona_id, line, hold_limit=hold_limit, clock=clock)


async def regular_round(
    resident: AssembledProfile, persona_id: str, at: datetime
) -> LifeMoment | None:
    """钟点上该来的一拍：常规节奏的一个信号，送到一条新的队列上，钟停在 ``at``；这一轮按
    ``resident``（:func:`resident`）跑，泳道是它的。"""
    from app.host import Signal
    from app.plugins.living import regular

    return await her_queue(persona_id, clock=clock_at(at)).take(
        regular(resident), Signal(at=at)
    )


async def nudged_round(
    resident: AssembledProfile, persona_id: str, at: datetime
) -> LifeMoment | None:
    """提前叫醒的一拍：送到一条新的队列上，钟停在 ``at``；这一轮按 ``resident`` 跑。"""
    from app.host import Signal
    from app.plugins.living import nudge

    return await her_queue(persona_id, clock=clock_at(at)).take(
        nudge(resident), Signal(at=at)
    )


@asynccontextmanager
async def her_round_runs(turns: Turns) -> AsyncIterator[None]:
    """她的这条队列上正有一个轮次在跑（挂住了也一样）：``with`` 里面它一直占着队列，出来它才
    跑完。"""
    import asyncio

    from app.host import Coalesce, Signal, WakeSource

    inside, finish = asyncio.Event(), asyncio.Event()

    async def due(participant, batch, clock) -> _Running:
        return _Running()

    async def run(_: _Running) -> None:
        inside.set()
        await finish.wait()

    running = asyncio.create_task(
        turns.take(
            WakeSource("running", Coalesce.QUEUE_EACH, due, run),
            Signal(at=datetime.now(UTC)),
        )
    )
    await inside.wait()
    try:
        yield
    finally:
        finish.set()
        await running


@dataclass(frozen=True)
class _Running:
    reason: str = "她正在跑的那一轮"


async def settled() -> None:
    """等刚发出的那几拍走到她的队列门口：排上了的在那儿等，被打发走的已经交回 ``None``。"""
    import asyncio

    for _ in range(50):
        await asyncio.sleep(0)


def her_tool(name: str) -> Tool:
    """她还没拆成插件的一只手：living 插件对象（:class:`app.plugins.living.NotSplitYet`）上的方法，
    包成跟插件登记给模型的同一种 :class:`~app.agent.tooling.Tool`。"""
    from app.agent.tooling import Tool
    from app.plugins.living import NotSplitYet

    return Tool(getattr(NotSplitYet(), name))


def glance_text(shown) -> str:
    """「看手机」交回来的那段文本。

    ``look_at_phone`` 交回的是一串内容块：第一项是她读到的那段会话，后面跟着别人发
    来的图（一句说明 + 一个图片块，见 :func:`app.living.phone._shown_picture`）。断
    言文本的用例只关心第一项，从这里取，各文件不各拆一遍。

    形状不对就当场炸：拿一个 ``@tool_error`` 的失败 dict 去 ``in`` 一个字串，结果是
    一条读起来像"她没看到这句话"的失败，而真相是这一眼根本没成。
    """
    assert isinstance(shown, list) and shown, f"这不是一份看手机的结果：{shown!r}"
    first = shown[0]
    assert first.get("type") == "text", f"第一项不是那段文本：{first!r}"
    return first["text"]


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
    它们建在这里而不是各个用例文件里，是因为**每一轮都会读手机的信封**——任何跑
    她一轮的用例都要用到，各建各的迟早会出现"这个文件建了那个没建"。

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
    from app.domain.session_transcript import SessionTranscript
    from app.living.day_page import LivingDayPage
    from app.living.mouth import SpokenOutbound
    from app.living.outgoing import OutgoingMessage, OutgoingResult, OutgoingUpTo
    from app.living.persona.chain import PersonaVersion
    from app.living.phone import PhoneRead
    from app.living.pictures import Picture
    from app.living.received import ReceivedMessage, ReceivedRead
    from app.living.records import Happening, Whereabouts
    from tests.runtime.conftest import migrate

    # ``LivingDayPage`` 跟手机那几张一样建在这里：**每一轮都读那一页**（``read_snapshot``
    # 把它当第二段摆给她），任何跑她一轮或 ``read_snapshot`` 的用例都要用到，
    # 各文件各建各的迟早会出现"这个文件建了那个没建"。``PersonaVersion`` 同一个理由：
    # 一轮、日记、开口三条路都要先问一遍"她是谁"（persona 服务读这条链）。
    # ``SessionTranscript`` 也是：每一轮开头读连续上下文、结尾写回下一版
    # （``app.agent.continuity``），少了它她的一轮第一步就炸。收到的消息那两张
    # 也是：每一轮都读她还没看过的收件、收尾时记下看过哪几条（``app.living.received``）。
    # 她往外发的那三张也是：每一轮开始前和结束时都把她做的事发出去（``app.living.outgoing``）。
    for cls in (
        Happening, Whereabouts, PhoneRead, SpokenOutbound, Picture,
        LivingDayPage, PersonaVersion, SessionTranscript,
        ReceivedMessage, ReceivedRead,
        OutgoingMessage, OutgoingResult, OutgoingUpTo,
    ):
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
def pinned(monkeypatch):
    """把几个群固定加白（走白名单读的那个 Dynamic Config key）。

    用例验的**不是**"她看不看得见这个群"时用它。会话白名单按"最近有没有人叫她"算，
    而群里不点名的消息一分都不算 —— 不按住这道闸的话，那些用例的群会从她视野里掉
    出去，用例红在一个跟它想说的事无关的地方。私聊没有这条（固定加白只对群生效），
    要让一条私聊在名单里就给它真的消息。

    **只截白名单那一个 key**，其余的照旧走真实读取：``dynamic_config`` 是进程内的同
    一个单例，整个 ``get`` 换掉的话这条用例里每一处配置读取（moment 多久、世界多久）拿
    到的都是这份 JSON 数组，各自解析失败、各自静默降级成默认值 —— 用例照绿，而它验
    的东西已经不是它以为的那个了。
    """
    import json

    from app.living import whitelist as whitelist_mod

    real_get = whitelist_mod.dynamic_config.get

    def pin(*channel_ids: str) -> None:
        payload = json.dumps(list(channel_ids))

        def fake_get(key, *, default=""):
            if key == whitelist_mod.LIVING_PINNED_GROUPS_KEY:
                return payload
            return real_get(key, default=default)

        monkeypatch.setattr(whitelist_mod.dynamic_config, "get", fake_get)

    return pin


@pytest.fixture
def in_a_moment():
    """把一个工具放进"她的某个 moment"里跑 —— 绑上她这一轮（:class:`app.agent.round.Round`）。

    工具体一律从 :func:`app.agent.round.current_round` 读她是谁、哪个泳道、几点、是哪一轮，没绑就
    ``LookupError``。用例只想验一个工具（看手机、发消息）时不必真跑一整轮，但**必须走真的绑定**，
    不然 lane 隔离、派生 id、moment 的身份全是假的。交回的就是这一轮。
    """
    import datetime as dt
    from contextlib import asynccontextmanager

    from app.agent.round import Round, in_round

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
        """``finishes=False`` = 这一轮崩在半路，收尾那一步没跑到。

        手机游标是**跟着这一轮落地的**（见 :func:`app.living.phone.commit_glances`），
        所以"这一轮跑完没有"是它的输入，不能不给。
        """
        at = now or dt.datetime(2026, 7, 25, 21, 30, tzinfo=cst)
        her_round = Round(
            app="agent-service",
            lane=lane,
            participant=persona_id,
            profile="resident",
            round_id=moment_id,
            now=at,
            reason="test",
            due=object(),
        )
        with in_round(her_round):
            yield her_round
        if finishes:
            from app.data.tx import tx
            from app.living.phone import Glances, commit_glances

            async with tx():
                await commit_glances(glances=her_round.state(Glances).pending)

    return bind


# ---------------------------------------------------------------------------
# 喂给模型的字符串里不许出现具体地名
#
# 她（和 world）唯一见过的地名样本，就是工具参数说明里的举例 —— **举例就是词表，会被
# 逐字抄走**。这件事炸过两次：
#
#   * 举例写 ``家/楼上/我房间``，于是绫奈和千凪两个人都把自己的卧室写成这一条全局
#     路径，2026-09-13 21:00 两人同一分钟落在同一个地名上，各自在自己屋里，判定却是
#     同处一室。
#   * 举例写 ``学校/二年三班教室``，被一字不差抄走，而设定集里那间教室叫
#     ``学校/初二三班教室`` —— 同一个地方从此分成两半。
#
# 换一批新的写死字符串只是把过期时间往后推：世界随时可以改名、删掉、重写那些地方，
# 而举例不会跟着变。所以这里钉的是**一个样本都不给**。
# ---------------------------------------------------------------------------

_PATH_SAMPLE = re.compile(r"\S/\S")

# 这一批曾经出现在工具说明、参数描述或报错文案里。列出来是因为它们是**世界的内容**，
# 不是格式：世界改名之后，代码里这几个字仍然在教模型写一个不存在的地方。
NAMES_ONCE_IN_TOOL_TEXT = (
    "我房间",
    "绫奈房间",
    "二年三班教室",
    "初二三班教室",
    "浴室",
    "操场",
    "客厅",
    "厨房",
    "文化祭",
    "教室",
    "走廊",
    "阳台",
    "灶台",
    "老街",
)


def path_samples(text: str) -> list[str]:
    """``text`` 里长得像一条具体路径的片段。

    判据是**斜杠两边都贴着字**：那时候它不是在说"层与层之间用这个符号隔开"，而是在
    把某一条具体路径摆给模型看。说清楚路径的形状不需要样本 —— 斜杠两边留空，说的是
    这个符号本身；贴着字，说的就是某一个具体的地方。

    这条判据顺带也挡住占位符（``A/B``、``某地/某处``、``<建筑>/<房间>``）：那些同样
    是在给一个可以照着填的模板，而且比直白的规则更难懂。

    唯一的例外是 ``「/」`` —— 那是在指这个符号自己（列目录时目录带的那个尾巴）。
    """
    return _PATH_SAMPLE.findall(text.replace("「/」", ""))


def names_of_places_in(text: str) -> list[str]:
    """``text`` 里出现的具体地名（:data:`NAMES_ONCE_IN_TOOL_TEXT` 那一批）。"""
    return [name for name in NAMES_ONCE_IN_TOOL_TEXT if name in text]


def model_facing_text(tool) -> dict[str, str]:
    """一只手交给模型的全部字符串：整段说明 + 每个参数的描述。

    按来源分开返回，红的时候一眼看得出是哪一段还留着样本。
    """
    texts = {"说明": tool.definition.description}
    props = tool.definition.parameters.get("properties", {})
    for name, schema in props.items():
        described = schema.get("description")
        if described:
            texts[f"参数 {name}"] = described
    return texts


@pytest.fixture(autouse=True)
def _prompts(prompts_from_fixtures):
    """一轮取齐的 prompt 来自回放的 prompt 快照（根 conftest 的 ``prompts_from_fixtures``）。"""
    return prompts_from_fixtures


# ---------------------------------------------------------------------------
# 模型那一步的替身
#
# 一轮里只有"调模型"那一步是假的：替身在模型客户端那一层（``app.agent.core.build_model_client``），
# 循环、工具派发、prompt 取齐和渲染、上下文存取全是真的。替身按这次调用的名字分
# （``current_call().agent``：她的一轮是 ``living_life_moment``，日记 ``living_day_page`` ……）。
# ---------------------------------------------------------------------------


def _as_returned(message) -> object:
    """工具交回的东西，从循环喂回模型的那条工具消息里还原：失败（``@tool_error`` 的 dict）和 dict
    在线上是 JSON 字符串，图片是一串块。"""
    import json

    content = message.content
    if isinstance(content, list):
        return [b.to_dict() for b in content]
    if content.startswith("{"):
        try:
            return json.loads(content)
        except ValueError:
            return content
    return content


class Calls:
    """一个 agent 的模型替身：每次调用先按顺序一只一只要写死的那几只手（循环真的派发它们），
    最后说 ``said``。

    记下每次调用拿到了什么（``runs``：SYSTEM 之后的那些消息，和一份
    ``{"values": 各段 prompt 编译时的变量（合在一起）, "call": 这次调用, "tools": 给它的工具名}``）、每只手交回了
    什么（``results``），以及每次调用最后那条输入（``prompts``）。``meanwhile`` 在模型那一步开头
    跑（模型跑着的时候发生的事），``after`` 在手都调完、说话之前跑；``fail`` 给了就在模型那一步
    抛它。
    """

    def __init__(self, *calls: tuple[str, dict], said: str = "继续") -> None:
        self.calls = list(calls)
        self.said = said
        self.runs: list[tuple[list, dict]] = []
        self.results: list[object] = []
        self.prompts: list[str] = []
        self.meanwhile = None
        self.after = None
        self.fail: BaseException | None = None

    def client(self):
        return _ScriptedCall(self)

    @property
    def material(self) -> str:
        """最后一次调用真的摆到眼前的那段文字（SYSTEM 之后的消息）。"""
        return "\n".join(m.content for m in self.runs[-1][0])

    @property
    def prompt_vars(self) -> dict[str, str]:
        """最后一次调用的 prompt 编译时拿到的变量。"""
        return self.runs[-1][1]["values"]


def _scripted_call_class():
    from app.agent.client import ModelClient
    from app.agent.neutral import Message, Role, ToolCall
    from app.agent.round import current_call
    from app.agent.trace import _current_prompts

    class ScriptedCall(ModelClient):
        def __init__(self, script: Calls) -> None:
            self._script = script
            self._step = 0

        async def complete(self, messages, *, tools=None, **_kwargs):
            script = self._script
            if self._step == 0:
                values: dict[str, str] = {}
                for prompt in _current_prompts.get():
                    values.update(getattr(prompt, "replay_variables", None) or {})
                script.runs.append(
                    (
                        list(messages[1:]),
                        {
                            "values": values,
                            "call": current_call(),
                            "tools": [t.name for t in tools or []],
                        },
                    )
                )
                script.prompts.append(messages[-1].content)
                if script.meanwhile is not None:
                    await script.meanwhile()
                if script.fail is not None:
                    raise script.fail
            elif messages[-1].role is Role.TOOL:
                script.results.append(_as_returned(messages[-1]))
            step, self._step = self._step, self._step + 1
            if step < len(script.calls):
                name, arguments = script.calls[step]
                return Message(
                    role=Role.ASSISTANT,
                    content="",
                    tool_calls=[ToolCall(id=f"call-{step}", name=name, arguments=arguments)],
                )
            if step == len(script.calls) and script.after is not None:
                await script.after()
            return Message(role=Role.ASSISTANT, content=script.said)

        async def structured(self, *_args, **_kwargs):  # pragma: no cover - not used here
            raise NotImplementedError

    return ScriptedCall


def _ScriptedCall(script: Calls):
    return _scripted_call_class()(script)


@pytest.fixture
def models(monkeypatch) -> dict:
    """每个 agent 的模型替身：名字 → 造替身的函数（一次调用造一个）。没给替身的 agent 一调模型
    就报错，不会悄悄连到真模型。"""
    from app.agent.round import current_call

    by_agent: dict = {}

    async def build_model_client(_model_id: str):
        agent = current_call().agent
        if agent not in by_agent:
            raise AssertionError(f"{agent!r} 调了模型，但用例没给它替身")
        return by_agent[agent]()

    monkeypatch.setattr("app.agent.core.build_model_client", build_model_client)
    return by_agent
