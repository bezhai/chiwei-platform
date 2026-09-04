"""会话白名单 —— 她视野里的那些会话，以及不在里面的那些整个不进她视野。

四条硬边界，这个文件逐条钉：

  * **规则之间是 or。** 固定加白的群、私聊消息总量过线、四个时间窗里有人叫她，命中
    任何一条就在名单里。所以会话少的 persona 不会被锁死 —— 有人跟她说话就命中时间窗。
  * **空名单不等于全放行，也不等于全挡住。** 固定加白那份配置读不到 / 配脏了就当空
    的，其余四档照常生效。
  * **算不出来就是不在名单里。** 统计查询炸了不能回退成"当作命中放行" —— 那道闸在
    最需要它的时候正好没有。
  * **一缝之内是同一份。** 名单跟"此刻几点"同性质：一缝里多处各算一次，她看到的会话
    集合会在一缝中途变化。

白名单只决定**哪些会话在她眼前**，不决定她要不要开口 —— 名单里那条会话她看不看、
回不回，仍然是她自己的判断。

**这个文件只钉判据本身**（几档、几条算够、算不出来怎么办、一缝里算几次）。闸真的管到
了哪几只手，用例跟着那几只手走，各在自己的文件里（那边有它们各自的替身和种子）：信封 /
nudge 那条钟 / 看手机 / 按名字找会话在 ``test_phone.py`` 最后一节，``send_message`` 在
``test_mouth.py``，读文件那两只手在 ``test_reading.py``，撤回**刻意不跟随**在
``test_takeback.py``。
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.living.phone import reachable_conversations
from app.living.whitelist import (
    DIRECT_TOTAL_OVER,
    IN_SIGHT_TIERS,
    LIVING_PINNED_GROUPS_KEY,
    load_pinned_groups,
    parse_pinned_groups,
)
from tests.living.test_phone import (
    _AKAO_BOT_UID,
    _DM,
    _GROUP,
    _OTHERS_DM,
    _SOMEONE,
    _at,
    _her_own,
    _incoming,
    _seed_world,
)

LANE = "coe-living"

# 这些用例的"现在"。四个窗口的下界都从它往回算，写死一个值，用例才说得清"恰好
# 几条"落在哪个窗口里。
_NOW = _at(21, 0)


async def _in_sight(*, persona_id: str = "akao", now: dt.datetime = _NOW) -> set[str]:
    """她这一刻看得见的那些会话（channel_id）。"""
    return {
        c.channel_id
        for c in await reachable_conversations(persona_id=persona_id, now=now)
    }


async def _they_said(
    conv: uuid.UUID,
    *,
    how_many: int,
    first_at: dt.datetime,
    names_bot: uuid.UUID | None = None,
) -> None:
    """真人在这条会话里连着说了几句（一分钟一句）。"""
    for i in range(how_many):
        await _incoming(
            conv,
            text_body=f"第{i}句",
            at=first_at + dt.timedelta(minutes=i),
            sender=_SOMEONE,
            sender_name="路人",
            names_bot=names_bot,
        )


# --------------------------------------------------------------------------
# 一 · 固定加白读配置：全对或全退
# --------------------------------------------------------------------------


def test_a_configured_list_of_groups_is_pinned():
    one = str(uuid.uuid4())
    two = str(uuid.uuid4())
    assert parse_pinned_groups(f'["{one}", "{two}"]') == frozenset({one, two})


def test_a_pinned_id_is_matched_however_it_was_typed():
    """配置里写成大写 / 带空格照样认得出是同一个群。

    库里那一列是 uuid，配置里是人手抄的串。两边不规范化成同一种写法的话，配置看着
    没错、而这个群一天都没进过名单，还一句报错都没有。
    """
    one = uuid.uuid4()
    assert parse_pinned_groups(f'[" {str(one).upper()} "]') == frozenset({str(one)})


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not json at all",
        '{"a": 1}',                                   # 不是数组
        '["not-a-uuid"]',                             # 不是会话 id
        "[123]",                                      # 不是字符串
        '[""]',                                       # 空串
        '["11111111-1111-4111-8111-111111111111", "nope"]',  # 半份对
    ],
)
def test_an_unusable_pin_list_pins_nothing_at_all(raw):
    """配脏了退回**空名单**，不是"半份能解析的那些"。

    白名单是可达性边界：半份解析成功比整份失败危险 —— 她会在一批说不清为什么的会话
    里能说话，而配置的人以为自己写的是另一批。整份要么全对要么全退（退回时打
    warning，见实现）。
    """
    assert parse_pinned_groups(raw) == frozenset()


async def test_the_pinned_groups_come_from_dynamic_config(monkeypatch):
    from app.living import whitelist as whitelist_mod

    seen: dict[str, str] = {}
    pinned = str(uuid.uuid4())

    def fake_get(key: str, *, default: str = "") -> str:
        seen["key"] = key
        return f'["{pinned}"]'

    monkeypatch.setattr(whitelist_mod.dynamic_config, "get", fake_get)

    assert await load_pinned_groups() == frozenset({pinned})
    assert seen["key"] == LIVING_PINNED_GROUPS_KEY


# --------------------------------------------------------------------------
# 二 · 四个时间窗：恰好几条算命中
# --------------------------------------------------------------------------
#
# 判据跟 nudge 那条钟同一份「在叫她」：私聊里每条真人消息都算，群里要点了她的名。
# 不这样的话私聊那四档恒为 0（私聊里没人 @ 她），"私聊也按上面的规则"就成了空话。


def test_the_tiers_are_the_ones_the_user_asked_for():
    """1h≥1 / 6h≥3 / 24h≥6 / 7d≥15，外加私聊总量 >30。

    阈值写死在代码里、不进配置：它们是这个功能的定义，不是运行参数。这条用例是那份
    定义的落点 —— 改阈值必须先改这里。
    """
    assert IN_SIGHT_TIERS == (
        (dt.timedelta(hours=1), 1),
        (dt.timedelta(hours=6), 3),
        (dt.timedelta(hours=24), 6),
        (dt.timedelta(days=7), 15),
    )
    assert DIRECT_TOTAL_OVER == 30


@pytest.mark.integration
async def test_one_message_in_the_last_hour_is_enough(living_db):
    """新加她的人说第一句话就在名单里 —— 她回得了。

    这是 or 关系的自然结果，不是漏：永久加白那条（总量 >30）对新人永远够不到，所以
    "陌生人能不能说上话"完全取决于四档窗口。
    """
    await _seed_world()
    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(minutes=30))

    assert str(_DM) in await _in_sight()


@pytest.mark.integration
async def test_a_line_that_went_quiet_drops_out_of_sight(living_db):
    """两小时前说过一句、之后没动静：一档都够不到，她看不见这条会话了。"""
    await _seed_world()
    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(hours=2))

    assert await _in_sight() == set()


@pytest.mark.integration
async def test_three_in_six_hours_is_enough_and_two_is_not(living_db):
    await _seed_world()
    await _they_said(_DM, how_many=2, first_at=_NOW - dt.timedelta(hours=5))
    assert await _in_sight() == set(), "6 小时里两条就把她拉回来了 —— 门槛是三条"

    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(hours=4))
    assert str(_DM) in await _in_sight()


@pytest.mark.integration
async def test_six_in_a_day_is_enough_and_five_is_not(living_db):
    await _seed_world()
    await _they_said(_DM, how_many=5, first_at=_NOW - dt.timedelta(hours=12))
    assert await _in_sight() == set(), "一天里五条不够 —— 门槛是六条"

    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(hours=10))
    assert str(_DM) in await _in_sight()


@pytest.mark.integration
async def test_fifteen_in_a_week_is_enough_and_fourteen_is_not(living_db):
    await _seed_world()
    await _they_said(_DM, how_many=14, first_at=_NOW - dt.timedelta(days=3))
    assert await _in_sight() == set(), "一周里十四条不够 —— 门槛是十五条"

    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(days=2))
    assert str(_DM) in await _in_sight()


@pytest.mark.integration
async def test_a_message_exactly_at_the_edge_of_the_window_is_outside_it(living_db):
    """窗口下界是开区间：正好一小时前那条不算在这一小时里。

    跟未读那条游标同一个开闭口径（``event_time > 下界``）。两处不一致的话，同一条
    消息在"她还没看"和"有人在找她"两个视角下答案不同。
    """
    await _seed_world()
    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(hours=1))

    assert await _in_sight() == set()


# --------------------------------------------------------------------------
# 三 · 私聊那条永久加白：只数真人发的
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_direct_line_past_thirty_messages_stays_in_sight(living_db):
    """聊了三十条以上的人，之后隔多久不说话她都还看得见这条私聊。

    时间窗全部够不到（消息都在一个月前），进名单的只能是这一条。
    """
    await _seed_world()
    await _they_said(_DM, how_many=31, first_at=_NOW - dt.timedelta(days=30))

    assert str(_DM) in await _in_sight()


@pytest.mark.integration
async def test_exactly_thirty_is_not_past_thirty(living_db):
    """用户原话是"大于 30"，所以三十条整不算。"""
    await _seed_world()
    await _they_said(_DM, how_many=30, first_at=_NOW - dt.timedelta(days=30))

    assert await _in_sight() == set()


@pytest.mark.integration
async def test_what_she_said_herself_does_not_count_toward_the_thirty(living_db):
    """她自己说了多少不说明对方在跟她聊。

    25 条真人 + 20 条她自己 = 45 条，按"总消息量"数早就过线了；只数真人发的就是 25，
    还差得远。
    """
    await _seed_world()
    long_ago = _NOW - dt.timedelta(days=30)
    await _they_said(_DM, how_many=25, first_at=long_ago)
    for i in range(20):
        await _her_own(
            _DM, text_body=f"我第{i}句", at=long_ago + dt.timedelta(hours=1, minutes=i)
        )

    assert await _in_sight() == set()


@pytest.mark.integration
async def test_a_group_has_no_permanent_pass_by_volume(living_db):
    """群里没有"聊得多就永久加白"这条 —— 用户原话里那条只给私聊。"""
    await _seed_world()
    await _they_said(
        _GROUP,
        how_many=40,
        first_at=_NOW - dt.timedelta(days=30),
        names_bot=_AKAO_BOT_UID,
    )

    assert await _in_sight() == set()


# --------------------------------------------------------------------------
# 四 · 群：得有人点她的名
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_group_nobody_named_her_in_is_not_in_sight(living_db):
    """群里刷屏刷得再凶，没人点她的名就不进她视野。

    这正是这个功能要收的东西：她挂在两百多个群里，绝大多数跟她没有关系。
    """
    await _seed_world()
    await _they_said(_GROUP, how_many=20, first_at=_NOW - dt.timedelta(minutes=30))

    assert await _in_sight() == set()


@pytest.mark.integration
async def test_a_group_that_named_her_in_the_last_hour_is_in_sight(living_db):
    await _seed_world()
    await _they_said(
        _GROUP,
        how_many=1,
        first_at=_NOW - dt.timedelta(minutes=30),
        names_bot=_AKAO_BOT_UID,
    )

    assert str(_GROUP) in await _in_sight()


@pytest.mark.integration
async def test_a_pinned_group_is_in_sight_with_nothing_in_it(living_db, pinned):
    """固定加白那几个群不看有没有人说话 —— 那就是"固定"的意思。"""
    await _seed_world()
    pinned(str(_GROUP))

    assert str(_GROUP) in await _in_sight()


@pytest.mark.integration
async def test_pinning_a_direct_line_does_nothing(living_db, pinned):
    """固定加白只对群生效。

    私聊那条永久加白是按消息量算的、不是点名的（用户原话是"固定的两个群"）。放宽成
    "群私聊都认"是擅自扩规则。
    """
    await _seed_world()
    pinned(str(_DM))

    assert await _in_sight() == set()


@pytest.mark.integration
async def test_an_empty_pin_list_is_not_a_free_pass(living_db):
    """一个群都没配 ≠ 全放行，也 ≠ 全挡住：其余四档照常。"""
    await _seed_world()
    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(minutes=10))
    await _they_said(_GROUP, how_many=5, first_at=_NOW - dt.timedelta(minutes=10))

    assert await _in_sight() == {str(_DM)}


# --------------------------------------------------------------------------
# 五 · 三个 persona 一视同仁
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_same_rules_apply_to_every_persona(living_db):
    """名单里没有"谁是谁"这回事：同一条判据，三个人各按自己的会话算。

    姐姐名下只有三五条会话，够不到任何门槛的话她们就哑了 —— 而规则之间是 or，有人
    跟她们说话就命中时间窗，所以不会被锁死。这条用例钉的就是"有人说话就看得见"。
    """
    await _seed_world()

    assert await _in_sight(persona_id="ayana") == set(), (
        "一条动静都没有，姐姐却看得见她的私聊"
    )

    await _they_said(_OTHERS_DM, how_many=1, first_at=_NOW - dt.timedelta(minutes=5))

    assert str(_OTHERS_DM) in await _in_sight(persona_id="ayana")


@pytest.mark.integration
async def test_a_persona_with_no_conversations_sees_nothing(living_db):
    await _seed_world()

    assert await _in_sight(persona_id="chinagi") == set()


# --------------------------------------------------------------------------
# 六 · 算不出来就是不在名单里
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_a_count_that_blew_up_leaves_the_conversation_out_of_sight(
    living_db, monkeypatch
):
    """统计查询炸了 = 这条会话这一缝不在名单里，**不是**当作命中放行。

    反过来做的话，这道闸在库最不健康的时候正好整个消失 —— 而那正是最需要它的时候。
    """
    from app.living import whitelist as whitelist_mod

    await _seed_world()
    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(minutes=5))
    assert str(_DM) in await _in_sight(), "用例前提没成立：这条本来该在名单里"

    async def boom(**_kw):
        raise RuntimeError("这条统计查询炸了")

    monkeypatch.setattr(whitelist_mod, "count_summons_since", boom)

    assert await _in_sight() == set()


@pytest.mark.integration
async def test_a_pin_list_that_cannot_be_read_does_not_silence_the_tiers(
    living_db, monkeypatch
):
    """固定加白读不到 ≠ 全哑。四档照常生效，掉出去的只有固定那几个群。

    旧的 ``feed_whitelist`` 是"配置挂 = 群聊全静音"，这里不能再来一遍。
    """
    from app.living import whitelist as whitelist_mod

    await _seed_world()
    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(minutes=5))
    await _they_said(
        _GROUP,
        how_many=1,
        first_at=_NOW - dt.timedelta(minutes=5),
        names_bot=_AKAO_BOT_UID,
    )

    monkeypatch.setattr(
        whitelist_mod.dynamic_config, "get", lambda key, *, default="": ""
    )
    assert await _in_sight() == {str(_DM), str(_GROUP)}

    monkeypatch.setattr(
        whitelist_mod.dynamic_config, "get", lambda key, *, default="": "{坏掉的}"
    )
    assert await _in_sight() == {str(_DM), str(_GROUP)}


# --------------------------------------------------------------------------
# 七 · 一缝之内是同一份
# --------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_list_does_not_change_in_the_middle_of_a_moment(
    living_db, in_a_moment
):
    """一缝里名单只定一次：中途来的消息不会让一条会话半路出现在她眼前。

    名单跟"此刻几点"同性质 —— 一缝之内必须是同一个值，否则她看到的会话集合会在这一
    缝中途变化：信封上没有的会话，她后半缝突然搜得到、发得出去。
    """
    await _seed_world()
    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(minutes=10))

    async with in_a_moment("akao", now=_NOW):
        first = await _in_sight()
        assert first == {str(_DM)}

        await _they_said(
            _GROUP,
            how_many=1,
            first_at=_NOW - dt.timedelta(minutes=1),
            names_bot=_AKAO_BOT_UID,
        )

        assert await _in_sight() == first, "名单在这一缝中途变了"

    # 缝外面（也就是下一缝）重新算，那个群就进来了。
    assert await _in_sight() == {str(_DM), str(_GROUP)}


@pytest.mark.integration
async def test_the_list_is_counted_once_in_a_moment(living_db, in_a_moment, monkeypatch):
    """一缝里问几次名单，统计只跑一次。

    这条钟一分钟一拍、她一缝里要问好几次（信封、看手机、找人、发消息、找文件），
    每次都重算就是把这个功能最贵的那一项乘上几倍。
    """
    from app.living import whitelist as whitelist_mod

    await _seed_world()
    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(minutes=10))

    real = whitelist_mod.count_summons_since
    calls: list[dict] = []

    async def counted(**kw):
        calls.append(kw)
        return await real(**kw)

    monkeypatch.setattr(whitelist_mod, "count_summons_since", counted)

    async with in_a_moment("akao", now=_NOW):
        await _in_sight()
        await _in_sight()
        await _in_sight()

    assert len(calls) == 1, f"一缝里统计跑了 {len(calls)} 次"
    assert len(calls[0]["since_ms"]) == len(IN_SIGHT_TIERS), (
        "四个窗口该一次算完，不是一档一次"
    )


@pytest.mark.integration
async def test_the_volume_rule_is_asked_as_a_yes_or_no_and_only_about_direct_lines(
    living_db, monkeypatch
):
    """总量那条单独问一次，问的是"够不够 31 条"，而且只问私聊。

    **它问的根本不是"有多少条"。** 问计数就得从第一条数到现在，下界只能写成 0，
    planner 拿不到有效范围只能整表扫（prod 实测 2026-09-05：akao 名下 69 条私聊，
    扫满 308 万行、960ms、101197 个 buffer）。问是非题则数到第 31 条就停，每条会话
    各走一次索引（1175 个 buffer 全部命中缓存）。跟四个窗口混在同一次调用里还会把
    那四个窗口一起拖下水 —— 这一条是那两个陷阱的门禁。
    """
    from app.living import whitelist as whitelist_mod

    await _seed_world()
    # 谁都够不到四个窗口：那条私聊才会走到"够不够"这一步。
    await _they_said(_DM, how_many=1, first_at=_NOW - dt.timedelta(days=20))

    real_windows = whitelist_mod.count_summons_since
    real_volume = whitelist_mod.find_conversations_others_spoke_in
    windows: list[dict] = []
    volume: list[dict] = []

    async def counted(**kw):
        windows.append(kw)
        return await real_windows(**kw)

    async def asked(**kw):
        volume.append(kw)
        return await real_volume(**kw)

    monkeypatch.setattr(whitelist_mod, "count_summons_since", counted)
    monkeypatch.setattr(whitelist_mod, "find_conversations_others_spoke_in", asked)
    await _in_sight()

    assert [call["since_ms"] for call in windows] == [
        [
            int((_NOW - window).timestamp() * 1000)
            for window, _ in IN_SIGHT_TIERS
        ]
    ], f"四个窗口的问法变了：{windows}"
    assert len(volume) == 1, f"总量那条问了 {len(volume)} 次：{volume}"
    assert volume[0]["at_least"] == DIRECT_TOTAL_OVER + 1, (
        "问的不是「至少 31 条」—— 规则是「多于 30 条」，翻译成闭区间要 +1"
    )
    assert {str(c["channel_id"]) for c in volume[0]["conversations"]} == {str(_DM)}, (
        "总量那一次问到了群头上 —— 群没有按量永久加白这条规则，白扫一遍"
    )


@pytest.mark.integration
async def test_a_volume_check_that_blew_up_leaves_the_line_out_of_sight(
    living_db, monkeypatch
):
    """问不出"够不够 31 条"时也是不在名单里 —— 跟四个窗口那半边同一条 fail-closed。

    两半各有一次查询，只堵一半的话另一半炸了会静默放行：那道闸在库最不健康的时候
    正好半边消失。
    """
    from app.living import whitelist as whitelist_mod

    await _seed_world()
    # 时间窗全部够不到，进名单只能靠总量那条。
    await _they_said(_DM, how_many=31, first_at=_NOW - dt.timedelta(days=30))
    assert str(_DM) in await _in_sight(), "用例前提没成立：这条本来该在名单里"

    async def boom(**_kw):
        raise RuntimeError("这条查询炸了")

    monkeypatch.setattr(whitelist_mod, "find_conversations_others_spoke_in", boom)

    assert await _in_sight() == set()
