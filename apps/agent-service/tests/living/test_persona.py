"""「她是谁」：版本链本身，以及链到底有没有走到她眼前.

persona 慢漂（周级 review）不 UPDATE ``bot_persona`` 主表，而是落 framework Data
版本链：每版带来源（seed＝出厂灌入 / review＝自动慢漂 / owner＝bezhai 干预），
照 WorldArc / LivingDayPage 模板：Key + narrative + written_at + Version、append-only、
读最新一版、整篇重写——新版**取代**旧版。

钉死的语义（docstring 层契约，本文件断言数据层行为）：

  * 读 a（读路径）：最新一版**不分来源**——owner 盖版即生效。
  * 读 b / 读 c（自动班的幂等与证据游标）：**只认 source='review'**——owner/seed
    版本既不挡当周自动班、也不推走证据窗口。
  * 周界 = 自然周一 00:00 CST（生活日是 04:00 界，但周界用自然周一零点，
    spec 决策 4）。
  * v0 灌入：链为空时把 ``bot_persona.persona_core`` 原文落为第一版
    （source='seed'）；链非空零操作，重跑无害。灌的必须是**链空时她实际读到的那
    份**——``persona_prompt_vars`` 退回的就是 ``persona_core``。

文件最后一节验的是**另一件事**：链上写下的东西真的到了她眼前。链写得再对，读侧
去读 ``bot_persona`` 那个扁平列的话，她自己改了三个月的正文一个字都不会出现——而
且没有任何报错，只是每一缝的底色都是出厂那份。

持久化用真实 Postgres（testcontainers）——版本链的正确性故事全在"能不能 append
进去、版本是否递增、来源过滤是否只认 review"，mock pg 等于什么都没测。
"""

from __future__ import annotations

from datetime import datetime

import pytest

import app.data.session as session_mod
from app.infra.cst_time import CST
from app.living.persona import (
    PersonaVersion,
    has_review_version_this_week,
    persona_prompt_vars,
    read_latest_persona_version,
    read_latest_review_written_at,
    seed_persona_chain,
    week_start_cst,
    write_persona_version,
)
from tests.runtime.conftest import migrate


@pytest.fixture
async def chain_db(test_db):
    await migrate(PersonaVersion, test_db)
    yield test_db


@pytest.fixture
async def seed_db(chain_db):
    """补上 ``bot_persona`` 主表（SQLAlchemy 表）——v0 灌入要从它读原文。"""
    from app.data.models import Base, BotPersona

    async with chain_db.begin() as conn:
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[BotPersona.__table__])
        )
    yield chain_db


async def _seed_bot_persona(
    persona_id: str,
    persona_lite: str,
    persona_core: str = "主表上那份出厂正文。",
) -> None:
    from app.data.models import BotPersona

    async with session_mod.get_session() as s:
        s.add(
            BotPersona(
                persona_id=persona_id,
                display_name="赤尾",
                persona_core=persona_core,
                persona_lite=persona_lite,
                default_reply_style="自然",
                error_messages={},
                appearance_detail="红发",
            )
        )


# ---------------------------------------------------------------------------
# Data 骨架（泳道隔离 + 自然键 + 不撞框架保留列）
# ---------------------------------------------------------------------------


def test_persona_version_key_is_lane_persona():
    """版本链自然键 = (lane, persona_id)：泳道隔离 + 每个角色一条链。"""
    from app.runtime.data import key_fields

    assert set(key_fields(PersonaVersion)) == {"lane", "persona_id"}


def test_persona_version_fields_avoid_framework_reserved_columns():
    """字段名不撞框架保留列（id / created_at / updated_at / dedup_hash）。

    写下时刻叫 ``written_at`` 而不是 ``created_at``——后者是框架的落库时刻，
    语义不同且是保留列（同 LivingDayPage / WorldAttention 教训）。
    """
    reserved = {"id", "created_at", "updated_at", "dedup_hash"}
    assert not reserved & set(PersonaVersion.model_fields)
    assert "written_at" in PersonaVersion.model_fields
    assert "source" in PersonaVersion.model_fields


# ---------------------------------------------------------------------------
# 周界：自然周一 00:00 CST（纯函数，先钉口径）
# ---------------------------------------------------------------------------


def test_week_start_is_monday_midnight_cst():
    """周三中午 → 本周一 00:00 CST。"""
    wednesday = datetime(2026, 6, 10, 12, 30, tzinfo=CST)
    assert week_start_cst(wednesday) == datetime(2026, 6, 8, 0, 0, tzinfo=CST)


def test_week_start_on_monday_just_after_midnight_is_same_day():
    """周一 00:01 → 当天 00:00（已进入新一周）。"""
    monday = datetime(2026, 6, 8, 0, 1, tzinfo=CST)
    assert week_start_cst(monday) == datetime(2026, 6, 8, 0, 0, tzinfo=CST)


def test_week_start_on_sunday_late_night_is_previous_monday():
    """周日 23:59 → 上一个周一 00:00（还在旧一周）。"""
    sunday = datetime(2026, 6, 7, 23, 59, tzinfo=CST)
    assert week_start_cst(sunday) == datetime(2026, 6, 1, 0, 0, tzinfo=CST)


def test_week_start_converts_other_timezones_to_cst_first():
    """跨时区先归一 CST 再取周界：UTC 周日 16:01 = CST 周一 00:01 → 新一周。"""
    from datetime import UTC

    sunday_utc = datetime(2026, 6, 7, 16, 1, tzinfo=UTC)
    assert week_start_cst(sunday_utc) == datetime(2026, 6, 8, 0, 0, tzinfo=CST)


# ---------------------------------------------------------------------------
# 读 a：真 PG 端到端（版本链 + owner 盖版即生效 + 隔离）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_write_then_read_latest_persona_version(chain_db):
    """写一版 → 按 (lane, persona_id) 读回最新（端到端 insert + 读回）。"""
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="她是刚考完高考的赤尾。",
        source="seed",
        written_at="2026-06-08T10:00:00+08:00",
    )

    latest = await read_latest_persona_version(lane="coe-t1", persona_id="akao")
    assert latest is not None
    assert latest.narrative == "她是刚考完高考的赤尾。"
    assert latest.source == "seed"
    assert latest.written_at == "2026-06-08T10:00:00+08:00"
    assert latest.version == 1


@pytest.mark.integration
async def test_versions_append_and_owner_supersedes(chain_db):
    """seed → review → owner 三版递增；读 a 不分来源——owner 盖版即生效。"""
    from app.runtime.persist import select_all_versions

    keys = {"lane": "coe-t1", "persona_id": "akao"}
    await write_persona_version(
        **keys,
        narrative="第一版：出厂正文。",
        source="seed",
        written_at="2026-06-08T10:00:00+08:00",
    )
    await write_persona_version(
        **keys,
        narrative="第二版：review 慢漂后的正文。",
        source="review",
        written_at="2026-06-08T11:00:00+08:00",
    )
    await write_persona_version(
        **keys,
        narrative="第三版：bezhai 干预盖掉的正文。",
        source="owner",
        written_at="2026-06-08T12:00:00+08:00",
    )

    versions = await select_all_versions(PersonaVersion, keys)
    assert [v.version for v in versions] == [1, 2, 3], (
        "版本链 append-only：版本逐次递增、旧版保留"
    )
    latest = await read_latest_persona_version(**keys)
    assert latest.source == "owner"
    assert latest.narrative == "第三版：bezhai 干预盖掉的正文。"


@pytest.mark.integration
async def test_read_latest_cold_chain_returns_none(chain_db):
    """没写过的 (lane, persona_id) 读回 None（冷启：读侧 fallback 主表）。"""
    assert (
        await read_latest_persona_version(lane="coe-t1", persona_id="akao") is None
    )


@pytest.mark.integration
async def test_persona_chain_lane_and_persona_isolation(chain_db):
    """泳道与 persona 隔离：coe 的版本绝不泄露到 prod、姐妹之间互不可见。"""
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="coe 里赤尾的一版。",
        source="seed",
        written_at="2026-06-08T10:00:00+08:00",
    )

    assert (
        await read_latest_persona_version(lane="prod", persona_id="akao") is None
    )
    assert (
        await read_latest_persona_version(lane="coe-t1", persona_id="ayana") is None
    )


# ---------------------------------------------------------------------------
# 读 b：本周是否已有 review 版本（只认 review——owner/seed 不挡班）
# ---------------------------------------------------------------------------

# 固定"现在"= 2026-06-09（周二）12:00 CST，本周一 = 2026-06-08 00:00 CST。
_NOW = datetime(2026, 6, 9, 12, 0, tzinfo=CST)


@pytest.mark.integration
async def test_review_this_week_true_when_review_written_in_week(chain_db):
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="本周慢漂的一版。",
        source="review",
        written_at="2026-06-08T05:00:00+08:00",
    )

    assert await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


@pytest.mark.integration
async def test_review_this_week_ignores_seed_and_owner(chain_db):
    """同周 seed + owner 版本在场仍返回 False——只认 review，不挡自动班。"""
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="本周灌入的出厂版。",
        source="seed",
        written_at="2026-06-08T05:00:00+08:00",
    )
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="本周 bezhai 盖的版。",
        source="owner",
        written_at="2026-06-09T09:00:00+08:00",
    )

    assert not await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


@pytest.mark.integration
async def test_review_last_sunday_night_does_not_count_this_week(chain_db):
    """周日 23:59 写的 review 归上一周：周界是自然周一 00:00 CST。"""
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="上周日深夜的一版。",
        source="review",
        written_at="2026-06-07T23:59:00+08:00",
    )

    assert not await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


@pytest.mark.integration
async def test_review_monday_just_after_midnight_counts_this_week(chain_db):
    """周一 00:01 写的 review 归本周（与上一条合起来钉死周一边界）。"""
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="周一凌晨的一版。",
        source="review",
        written_at="2026-06-08T00:01:00+08:00",
    )

    assert await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


@pytest.mark.integration
async def test_review_this_week_cold_chain_is_false(chain_db):
    assert not await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )


# ---------------------------------------------------------------------------
# 读 c：最新一条 review 版本的 written_at（证据游标，owner/seed 不动游标）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_latest_review_written_at_ignores_seed_and_owner(chain_db):
    """seed → review → owner 之后，游标 = review 那版的 written_at（不被 owner 推走）。"""
    keys = {"lane": "coe-t1", "persona_id": "akao"}
    await write_persona_version(
        **keys,
        narrative="出厂版。",
        source="seed",
        written_at="2026-06-01T10:00:00+08:00",
    )
    await write_persona_version(
        **keys,
        narrative="慢漂版。",
        source="review",
        written_at="2026-06-08T05:00:00+08:00",
    )
    await write_persona_version(
        **keys,
        narrative="bezhai 盖版。",
        source="owner",
        written_at="2026-06-09T09:00:00+08:00",
    )

    assert (
        await read_latest_review_written_at(**keys) == "2026-06-08T05:00:00+08:00"
    )


@pytest.mark.integration
async def test_latest_review_written_at_none_when_no_review(chain_db):
    """链上只有 seed / owner（或链为空）→ None：首跑窗口 = 全部现存页。"""
    assert (
        await read_latest_review_written_at(lane="coe-t1", persona_id="akao") is None
    )

    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="出厂版。",
        source="seed",
        written_at="2026-06-01T10:00:00+08:00",
    )
    assert (
        await read_latest_review_written_at(lane="coe-t1", persona_id="akao") is None
    )


# ---------------------------------------------------------------------------
# v0 灌入：链为空时把 bot_persona.persona_core 落为第一版（source='seed'），幂等
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_seed_persona_chain_copies_persona_core_verbatim(seed_db):
    """首跑：bot_persona.persona_core 原文一字不差落为第一版 seed。

    灌的是 ``persona_core`` 而不是 ``persona_lite``：链空时
    :func:`persona_prompt_vars` 退回的就是 ``persona_core``，起点那一版必须等于她
    当时**实际读到的**那份。灌错列不会报错——链上的历史只是从 v1 起就是断的：v1
    记着一段她从没读过的东西，而 v2 是在她真正读到的那份上改出来的。
    """
    await _seed_bot_persona(
        "akao",
        "lite：这一列不该被灌进链里。",
        persona_core="出厂身份正文：她是住在杭州的赤尾。",
    )

    assert await seed_persona_chain(lane="coe-t1", persona_id="akao") is True

    latest = await read_latest_persona_version(lane="coe-t1", persona_id="akao")
    assert latest is not None
    assert latest.narrative == "出厂身份正文：她是住在杭州的赤尾。"
    assert "lite" not in latest.narrative, (
        "灌的是 persona_lite —— 起点那一版记的是她从没读过的东西"
    )
    assert latest.source == "seed"
    assert latest.version == 1


@pytest.mark.integration
async def test_the_seeded_version_is_what_she_was_already_reading(seed_db):
    """灌完之后她读到的东西一个字都没变 —— 起点那一版就是链空时的 fallback。

    这条是上一条的另一面：不比对列名，比对**她眼前那段文字**在灌入前后是否一致。
    灌错列的话这里会当场变一段她从没读过的正文，而没有任何报错。
    """
    await _seed_bot_persona("akao", "lite：这一列不该被灌进链里。")

    before = await persona_prompt_vars(lane="coe-t1", persona_id="akao")
    await seed_persona_chain(lane="coe-t1", persona_id="akao")
    after = await persona_prompt_vars(lane="coe-t1", persona_id="akao")

    assert after == before


@pytest.mark.integration
async def test_seed_persona_chain_is_idempotent(seed_db):
    """连跑两次只有一行：链非空零操作，重跑无害。"""
    from app.runtime.persist import select_all_versions

    await _seed_bot_persona("akao", "出厂身份正文。")

    assert await seed_persona_chain(lane="coe-t1", persona_id="akao") is True
    assert await seed_persona_chain(lane="coe-t1", persona_id="akao") is False

    versions = await select_all_versions(
        PersonaVersion, {"lane": "coe-t1", "persona_id": "akao"}
    )
    assert len(versions) == 1


@pytest.mark.integration
async def test_seed_persona_chain_noop_when_chain_already_has_versions(seed_db):
    """链上已有任何版本（哪怕是 review/owner）→ 灌入零操作，不覆盖现状。"""
    await _seed_bot_persona("akao", "出厂身份正文。")
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="已有的 review 版。",
        source="review",
        written_at="2026-06-08T05:00:00+08:00",
    )

    assert await seed_persona_chain(lane="coe-t1", persona_id="akao") is False

    latest = await read_latest_persona_version(lane="coe-t1", persona_id="akao")
    assert latest.narrative == "已有的 review 版。"


@pytest.mark.integration
async def test_seed_persona_chain_missing_bot_persona_fails_fast(seed_db):
    """bot_persona 没有这行 → fail fast（没有原文可灌，不静默写空版）。"""
    with pytest.raises(ValueError):
        await seed_persona_chain(lane="coe-t1", persona_id="ghost")


@pytest.mark.integration
async def test_seed_version_does_not_satisfy_review_idempotency(seed_db):
    """灌入的 seed 版不算 review：读 b 仍 False、读 c 仍 None（自动班照常跑）。"""
    await _seed_bot_persona("akao", "出厂身份正文。")
    await seed_persona_chain(lane="coe-t1", persona_id="akao")

    assert not await has_review_version_this_week(
        lane="coe-t1", persona_id="akao", now=_NOW
    )
    assert (
        await read_latest_review_written_at(lane="coe-t1", persona_id="akao") is None
    )


# ---------------------------------------------------------------------------
# 读侧：链上那一版有没有真的到她眼前（``{{persona_core}}`` 的值从哪来）
# ---------------------------------------------------------------------------
#
# 链写对了不等于她读得到。上面每一条验的都是"写进去、读回来"，而她真正看到的是
# :func:`persona_prompt_vars` 摆出来的那两个 prompt 变量——中间任何一处去读
# ``bot_persona.persona_core`` 那个扁平列，她自己改了三个月的正文就一个字都不会
# 出现，而且**一句报错都没有**：每一缝照跑，只是底色永远是出厂那份。


@pytest.mark.integration
async def test_what_she_wrote_herself_is_what_reaches_her(seed_db):
    """链上有版本时，喂进 prompt 的是最新那一版的 narrative，不是扁平列。"""
    await _seed_bot_persona("akao", "出厂身份正文。")
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="她今年不拍胶片了，改成每周写一篇角色分析。",
        source="review",
        written_at="2026-06-08T05:00:00+08:00",
    )

    got = await persona_prompt_vars(lane="coe-t1", persona_id="akao")

    assert got["persona_core"] == "她今年不拍胶片了，改成每周写一篇角色分析。", (
        "她读到的还是 bot_persona 那个扁平列 —— 版本链在读侧根本没接上"
    )
    assert got["persona_name"] == "赤尾"


@pytest.mark.integration
async def test_an_empty_chain_falls_back_to_the_flat_column(seed_db):
    """全新泳道链上一版都没有 → 退回 ``bot_persona.persona_core``（冷启不空手）。"""
    await _seed_bot_persona("akao", "出厂身份正文。")

    got = await persona_prompt_vars(lane="coe-t1", persona_id="akao")

    assert got["persona_core"] == "主表上那份出厂正文。"


@pytest.mark.integration
async def test_with_nothing_written_anywhere_she_is_told_so(seed_db):
    """链空 + 扁平列也空白 → 说实话，不渲染出一个空洞。

    空洞的后果是静默的：prompt 里那一段变成空行，她这一缝没有可对照的底色，而
    模型不会因此报错——只会表现成"她想不起自己是个什么样的人"。
    """
    await _seed_bot_persona("akao", "出厂身份正文。", persona_core="   ")

    got = await persona_prompt_vars(lane="coe-t1", persona_id="akao")

    assert got["persona_core"].strip() != ""


@pytest.mark.integration
async def test_a_blank_version_does_not_blank_her_out(seed_db):
    """链上最新一版正文空白 → 当成没有，退回扁平列。

    写侧拦得住空白落版，但 owner 是人工写入口。这里是防御纵深：宁可退回出厂那份，
    也不能让她这一缝拿着一段空白当自己。
    """
    await _seed_bot_persona("akao", "出厂身份正文。")
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="   ",
        source="owner",
        written_at="2026-06-08T05:00:00+08:00",
    )

    got = await persona_prompt_vars(lane="coe-t1", persona_id="akao")

    assert got["persona_core"] == "主表上那份出厂正文。"


@pytest.mark.integration
async def test_the_newest_version_wins_even_when_written_at_goes_backwards(seed_db):
    """"最新"按 ``version`` 算，不按 ``written_at``。

    ``written_at`` 是**调用方给的字符串**（owner 人工盖版可以填任何时刻），乱序完全
    可能。按它取最新的话，一次填错时刻的人工干预会让链从此永远停在那一版上。
    """
    await _seed_bot_persona("akao", "出厂身份正文。")
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="先落地的那一版。",
        source="review",
        written_at="2026-06-09T10:00:00+08:00",
    )
    await write_persona_version(
        lane="coe-t1",
        persona_id="akao",
        narrative="后落地的那一版（written_at 反而更早）。",
        source="owner",
        written_at="2026-06-01T10:00:00+08:00",
    )

    got = await persona_prompt_vars(lane="coe-t1", persona_id="akao")

    assert got["persona_core"] == "后落地的那一版（written_at 反而更早）。"


@pytest.mark.integration
async def test_another_lanes_version_never_reaches_this_one(seed_db):
    """prod 那条链上的版本读不到 coe 里来 —— 键上带 lane 就是为了这个。

    漏了 lane 的后果是双向的：coe 里跑实验改出来的人设会当场生效在 prod 的她身上，
    而这件事在库里看不出来（表里两条链都在，只是读的时候挑错了行）。
    """
    await _seed_bot_persona("akao", "出厂身份正文。")
    await write_persona_version(
        lane="prod",
        persona_id="akao",
        narrative="prod 上那一版。",
        source="review",
        written_at="2026-06-08T05:00:00+08:00",
    )

    got = await persona_prompt_vars(lane="coe-living", persona_id="akao")

    assert got["persona_core"] == "主表上那份出厂正文。"


@pytest.mark.integration
async def test_a_persona_the_table_never_heard_of_still_gets_two_variables(seed_db):
    """库里没有这个人也不让这一轮跑不起来：名字退回 persona_id，正文说实话。"""
    got = await persona_prompt_vars(lane="coe-t1", persona_id="ghost")

    assert got["persona_name"] == "ghost"
    assert got["persona_core"].strip() != ""


@pytest.mark.integration
async def test_the_variables_are_exactly_the_two_the_prompts_name(seed_db):
    """键名就是 Langfuse 上三个 prompt 正文里写着的那两个。

    改键名不会报错，只会让 ``{{persona_core}}`` 原样渲染成字面量出现在她眼前。
    """
    await _seed_bot_persona("akao", "出厂身份正文。")

    got = await persona_prompt_vars(lane="coe-t1", persona_id="akao")

    assert set(got) == {"persona_name", "persona_core"}


def test_all_three_paths_ask_the_same_place_who_she_is():
    """一缝、写日记、开口渲染读的是**同一个**函数。

    三处各拼一份的后果不是报错，是分裂：她那一缝里是链上新的自己，一开口又变回
    ``bot_persona`` 上出厂那份。开口那条路原先就是各拼一份，而且空白处理跟另外两处
    还不一样。
    """
    from app.living import day_page as page_mod
    from app.living import moment as moment_mod
    from app.living import mouth as mouth_mod

    for mod in (moment_mod, page_mod, mouth_mod):
        assert mod.persona_prompt_vars is persona_prompt_vars, (
            f"{mod.__name__} 没走同一个入口"
        )
        assert not hasattr(mod, "find_persona"), (
            f"{mod.__name__} 自己又查了一遍 bot_persona —— 第二份人设组装正在长出来"
        )
