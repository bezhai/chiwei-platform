"""她手边那几份写好的说明 —— 读得到，也跑得动说明里教她跑的东西。

替身只装在**最外面那一跳**（沙箱那次 HTTP）。注册表、加载器、渲染器全都是真的：
说明真的写到盘上、真的被扫进注册表、正文真的过一遍渲染。上一版那两个工具就是因为
中间没有任何一层被真的跑过，「``sandbox_bash`` 从来不传 ``skill_name``」这条缺陷在
它自己的单测里一次都照不出来。

五条硬边界，各有对应的用例：

  * **她知道有哪些可读。** 清单只能从 prompt 变量进 —— 工具 schema 在 import 时定死，
    而注册表是启动时填、每 30 秒热加载的。
  * **一份都没有的时候说一句实话**，不是渲染出一个空洞：她那段说明里留一个空标题，
    她读到的就是"这里本该有东西而它没了"。
  * **跑命令必须带上是哪份说明教的**，否则那份说明自己的脚本不在她手边 —— 说明通篇
    教她跑的那条命令当场找不到文件。
  * **跑不通就把退出码和报错原样给她**，不翻译、不吞。
  * **说明书里不许写不存在的限制。** 旧那句「限制：无网络访问」是错的：沙箱按命令名
    封 ``curl`` / ``wget`` 这些，Python 脚本自己发 HTTP 不受这条管。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.capabilities.sandbox import OUTPUT_CUT_MARK, SandboxResult
from app.living import guides as guides_mod
from app.living.guides import (
    GUIDE_TOOLS,
    GUIDES_VAR,
    guides_she_can_read,
    read_a_guide,
    run_a_script,
)
from app.skills import renderer as renderer_mod
from app.skills.registry import SkillRegistry

# 盘上那份说明长什么样（NFS 上四份的形状：YAML frontmatter + 正文）。
_DRAWING = """---
name: drawing
description: 人物画图指南 — 涉及三姐妹或 Cosplay 时加载
---

## 姐妹三人体貌参考

赤尾：黑长直，眼尾偏下。
"""

# 带一条预处理指令 + 通篇 $SKILL_DIR 的那种（NFS 上三份是这样）。
_BANGUMI = """---
name: bangumi
description: 搜索 Bangumi 上的动画、书籍、游戏等 ACG 条目
---

## 用法

!`python3 $SKILL_DIR/scripts/bangumi.py --help`

搜条目：`python3 $SKILL_DIR/scripts/bangumi.py search <关键词>`
"""


@pytest.fixture
def shelf(tmp_path: Path):
    """把几份说明真的写到盘上、真的加载进注册表；跑完清空。

    注册表是 class-level 的全局状态：不清的话这一条用例装的几份会漏进别的用例，
    而"一份都没有"那几条正好验的是空注册表。清空走 ``load_all`` 一个不存在的目录
    —— 它自己第一步就是清空，跟线上目录挂不上时走的是同一条路。
    """

    def install(**bodies: str) -> Path:
        for name, body in bodies.items():
            folder = tmp_path / name
            folder.mkdir(exist_ok=True)
            (folder / "SKILL.md").write_text(body, encoding="utf-8")
        SkillRegistry.load_all(tmp_path)
        return tmp_path

    yield install
    SkillRegistry.load_all(tmp_path / "这个目录不存在")


@pytest.fixture
def sandbox(monkeypatch):
    """替身沙箱：记下每次跑的是什么、带没带说明名，交回摆好的结果。

    两处都换成同一个替身 —— 她自己跑命令那只手，和渲染器执行说明里预处理指令那一
    次。两处走的是同一个 capability，替身也该是同一个，不然"预处理有没有带上说明名"
    这条根本看不见。
    """

    class Ran:
        def __init__(self) -> None:
            self.calls: list[dict] = []

    rec = Ran()

    def install(
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        dropped: int = 0,
        boom=None,
    ) -> Ran:
        async def fake_run(*, command, skill_name="", envs=None, timeout=30):
            rec.calls.append({"command": command, "skill_name": skill_name})
            if boom is not None:
                raise boom
            return SandboxResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                dropped=dropped,
            )

        monkeypatch.setattr(guides_mod, "run", fake_run)
        monkeypatch.setattr(renderer_mod, "run", fake_run)
        return rec

    return install


# --------------------------------------------------------------------------
# 一 · 读得到
# --------------------------------------------------------------------------


async def test_reading_one_puts_the_whole_thing_in_front_of_her(
    shelf, in_a_moment
):
    """正文原样进她眼前 —— 她长什么样就写在其中一份里。"""
    shelf(drawing=_DRAWING)

    async with in_a_moment("akao", finishes=False):
        body = await read_a_guide.invoke({"which": "drawing"})

    assert isinstance(body, str), f"读回来的不是正文：{body!r}"
    assert "黑长直" in body


async def test_a_guide_that_teaches_a_script_comes_back_ready_to_run(
    shelf, sandbox, in_a_moment
):
    """带脚本那几份：预处理真的跑过，而且路径是她照抄就能用的那种。

    ``$SKILL_DIR`` 留在正文里的话，她照抄那条命令跑出来是一个空路径。
    """
    shelf(bangumi=_BANGUMI)
    rec = sandbox(stdout="usage: bangumi.py [-h] {search,subject}")

    async with in_a_moment("akao", finishes=False):
        body = await read_a_guide.invoke({"which": "bangumi"})

    assert isinstance(body, str), f"读回来的不是正文：{body!r}"
    assert "$SKILL_DIR" not in body, "路径还是个变量，她照抄跑不通"
    assert "/sandbox/skills/bangumi/scripts/bangumi.py" in body
    assert "usage: bangumi.py" in body, "预处理的结果没进正文"
    assert [c["skill_name"] for c in rec.calls] == ["bangumi"]


async def test_a_name_she_got_wrong_comes_back_with_the_ones_she_can_read(
    shelf, in_a_moment
):
    """名字写错了要说得出能读的有哪些，不然她只能瞎猜第二次。"""
    shelf(drawing=_DRAWING, bangumi=_BANGUMI)

    async with in_a_moment("akao", finishes=False):
        outcome = await read_a_guide.invoke({"which": "drawin"})

    assert isinstance(outcome, dict), "读了一份不存在的说明却当成读成了"
    said = outcome["message"]
    assert "drawing" in said and "bangumi" in said


async def test_nothing_on_hand_says_so_instead_of_blaming_her(
    shelf, in_a_moment
):
    """目录挂不上时注册表是空的 —— 那不是她名字写错了，别让她一直重试。"""
    shelf()

    async with in_a_moment("akao", finishes=False):
        outcome = await read_a_guide.invoke({"which": "drawing"})

    assert isinstance(outcome, dict)
    assert "一份说明都没有" in outcome["message"]


async def test_reading_outside_a_moment_does_not_quietly_work(shelf):
    """没绑在一轮上就是 wiring 坏了，得当场看得见。"""
    shelf(drawing=_DRAWING)

    outcome = await read_a_guide.invoke({"which": "drawing"})

    assert isinstance(outcome, dict), f"没在一轮里也照读了：{outcome!r}"


# --------------------------------------------------------------------------
# 二 · 知道有哪些可读
# --------------------------------------------------------------------------


def test_the_list_names_every_one_she_can_read(shelf):
    shelf(drawing=_DRAWING, bangumi=_BANGUMI)

    listed = guides_she_can_read()

    for word in ("drawing", "人物画图指南", "bangumi", "ACG 条目"):
        assert word in listed, f"清单里没有 {word}：{listed!r}"


def test_an_empty_shelf_still_says_something(shelf):
    """一份都没有时**绝不能**是空串：她那段说明会渲染成一个空标题。"""
    shelf()

    listed = guides_she_can_read()

    assert listed.strip(), "清单渲染成了空的"
    assert "一份说明都没有" in listed


def test_the_list_follows_the_shelf_instead_of_being_frozen_in_the_schema(shelf):
    """清单只能从 prompt 变量进：注册表每 30 秒热加载，schema 在 import 时就定死了。

    所以这里换一份盘上的内容，她下一轮读到的清单必须跟着变；同时工具参数里不许出现
    一份写死的名字清单（``enum``）—— 那等于把这份会变的东西焊进 schema。
    """
    shelf(drawing=_DRAWING)
    assert "bangumi" not in guides_she_can_read()

    shelf(bangumi=_BANGUMI)

    assert "bangumi" in guides_she_can_read()
    which = read_a_guide.definition.parameters["properties"]["which"]
    assert "enum" not in which, "可读的有哪些被焊进了 schema，热加载之后就是假的"


# --------------------------------------------------------------------------
# 三 · 跑得动
# --------------------------------------------------------------------------


async def test_running_a_script_carries_the_guide_name_down_to_the_sandbox(
    shelf, sandbox, in_a_moment
):
    """不带这个名字，那份说明的脚本就不在她跑命令的地方 —— 旧工具烂在这一条上。"""
    shelf(bangumi=_BANGUMI)
    rec = sandbox(stdout="孤独摇滚！ (2022)\n")

    async with in_a_moment("akao", finishes=False):
        out = await run_a_script.invoke(
            {
                "command": "python3 /sandbox/skills/bangumi/scripts/bangumi.py search 孤独摇滚",
                "guide": "bangumi",
            }
        )

    assert rec.calls == [
        {
            "command": "python3 /sandbox/skills/bangumi/scripts/bangumi.py search 孤独摇滚",
            "skill_name": "bangumi",
        }
    ]
    assert "孤独摇滚！ (2022)" in out


async def test_a_bit_of_arithmetic_needs_no_guide_at_all(
    shelf, sandbox, in_a_moment
):
    shelf(bangumi=_BANGUMI)
    rec = sandbox(stdout="42\n")

    async with in_a_moment("akao", finishes=False):
        out = await run_a_script.invoke({"command": 'python3 -c "print(6*7)"'})

    assert rec.calls[0]["skill_name"] == ""
    assert "42" in out


async def test_a_guide_name_she_got_wrong_stops_before_the_sandbox(
    shelf, sandbox, in_a_moment
):
    """带一个不存在的名字下去，沙箱那边只是安静地不软链 —— 报错会指向文件找不到，
    而真正错的是这个名字。在这儿就拦住，并且告诉她能读的有哪些。"""
    shelf(bangumi=_BANGUMI)
    rec = sandbox(stdout="不该跑到这儿")

    async with in_a_moment("akao", finishes=False):
        outcome = await run_a_script.invoke(
            {"command": "python3 scripts/bangumi.py --help", "guide": "bangumii"}
        )

    assert isinstance(outcome, dict), "名字写错了还是跑了出去"
    assert "bangumi" in outcome["message"]
    assert rec.calls == [], "错的名字已经打到沙箱去了"


async def test_a_command_that_failed_hands_her_the_exit_code_and_the_error(
    shelf, sandbox, in_a_moment
):
    """跑不通是她要看见的事实：退出码、打出来的、报错，一样都不能吞。"""
    shelf(bangumi=_BANGUMI)
    sandbox(
        exit_code=2,
        stdout="开始查询\n",
        stderr="Traceback: KeyError: 'BANGUMI_TOKEN'",
    )

    async with in_a_moment("akao", finishes=False):
        out = await run_a_script.invoke(
            {"command": "python3 scripts/bangumi.py search x", "guide": "bangumi"}
        )

    assert isinstance(out, str), f"跑不通被当成了工具自己坏了：{out!r}"
    assert "2" in out
    assert "开始查询" in out
    assert "BANGUMI_TOKEN" in out


async def test_a_command_that_printed_nothing_says_so(
    shelf, sandbox, in_a_moment
):
    """空字符串交回去，她看到的是一个什么都没有的工具结果，分不清是没跑还是没输出。"""
    shelf(bangumi=_BANGUMI)
    sandbox(stdout="   ")

    async with in_a_moment("akao", finishes=False):
        out = await run_a_script.invoke({"command": "true"})

    assert isinstance(out, str) and out.strip(), f"交回了一片空白：{out!r}"


async def test_a_cut_result_reaches_her_with_the_notice_still_on_it(
    shelf, sandbox, in_a_moment, caplog
):
    """裁在 capability 那一层（两条路都要过它），这只手只负责别把那句提示弄丢。

    顺带留一条带 moment 身份的痕：事后要查得出哪一轮被截过。langfuse 会系统性丢 trace，
    这条日志是唯一查得到的地方。
    """
    shelf(bangumi=_BANGUMI)
    capped = "x" * 40 + f"\n\n……（后面还有 96000 {OUTPUT_CUT_MARK} —— 略）"
    sandbox(stdout=capped, dropped=96_000)

    with caplog.at_level(logging.INFO, logger="app.living.guides"):
        async with in_a_moment("akao", finishes=False):
            out = await run_a_script.invoke({"command": "python3 -c ..."})

    assert OUTPUT_CUT_MARK in out, "她读到的是一段戛然而止的输出，不知道后面还有"
    cut_lines = [
        r.getMessage() for r in caplog.records if "96000" in r.getMessage()
    ]
    assert cut_lines, f"截断没留下任何痕迹：{[r.getMessage() for r in caplog.records]}"
    assert "akao" in cut_lines[0] and "2026-07-25T21:30+08:00" in cut_lines[0]


async def test_a_guide_whose_preprocessing_flooded_leaves_a_trace_too(
    shelf, sandbox, in_a_moment, caplog
):
    """说明里那条 ``!`cmd``` 的结果被替换进正文，那一路同样可能被裁。

    她读到的那份说明因此是不全的 —— 事后得查得出是哪一轮、哪一份。
    """
    shelf(bangumi=_BANGUMI)
    sandbox(stdout=f"usage: ...\n……（后面还有 70000 {OUTPUT_CUT_MARK} —— 略）")

    with caplog.at_level(logging.INFO, logger="app.living.guides"):
        async with in_a_moment("akao", finishes=False):
            body = await read_a_guide.invoke({"which": "bangumi"})

    assert OUTPUT_CUT_MARK in body
    marked = [
        r.getMessage()
        for r in caplog.records
        if "bangumi" in r.getMessage() and "截" in r.getMessage()
    ]
    assert marked, f"没留下痕迹：{[r.getMessage() for r in caplog.records]}"
    assert "2026-07-25T21:30+08:00" in marked[0]


def test_both_hands_tell_her_the_output_can_be_cut():
    """她得知道自己读到的可能不是全部，以及那时该怎么办。

    不说的话她会把截过的那一段当成完整结果，然后基于一个不完整的东西往下做事 ——
    跟 ``app.living.day_page`` 里那条"留白她会以为材料被截断了"是同一类。
    """
    for t in GUIDE_TOOLS:
        assert "太长会被截掉" in t.definition.description, (
            f"{t.name} 没告诉她输出可能不全"
        )
    assert "head" in run_a_script.definition.description, (
        "没告诉她被截了之后该怎么办"
    )


async def test_running_outside_a_moment_does_not_quietly_work(shelf, sandbox):
    shelf(bangumi=_BANGUMI)
    rec = sandbox(stdout="不该跑到这儿")

    outcome = await run_a_script.invoke({"command": "echo hi"})

    assert isinstance(outcome, dict), f"没在一轮里也照跑了：{outcome!r}"
    assert rec.calls == []


# --------------------------------------------------------------------------
# 四 · 说明书本身
# --------------------------------------------------------------------------


def test_neither_hand_ever_asks_her_how_long_something_takes():
    """真人对「多久」没有内感受。``tests/living/test_moment.py`` 对整份工具集有同一
    条；这两只手自己也守一遍，改坏了在本文件就该红。"""
    banned = ("minute", "duration", "how_long", "seconds", "until", "hour")
    for t in GUIDE_TOOLS:
        for pname in t.definition.parameters.get("properties", {}):
            assert not any(b in pname.lower() for b in banned), (
                f"{t.name} 的参数 {pname} 在问她一个时长"
            )


def test_neither_description_claims_a_limit_that_is_not_real():
    """旧那句「限制：无网络访问」是错的。

    沙箱按**命令名**封 ``curl`` / ``wget`` / ``ssh`` 这些，没有网络命名空间隔离
    —— 说明里的 Python 脚本自己发 HTTP 照样通，那三份说明就靠这条才成立。写一条不
    存在的限制，她会因此不去跑本来跑得通的东西。
    """
    for t in GUIDE_TOOLS:
        assert "无网络" not in t.definition.description, (
            f"{t.name} 的说明书里写着一条不存在的限制"
        )


def test_the_hand_that_runs_things_points_back_at_the_guides():
    """两只手是一件事的两半：说明里教她跑，她才跑。说明书里指不回去的话，她读完
    一份满是脚本的说明，手上没有能跑它的东西。"""
    assert "run_a_script" in read_a_guide.definition.description
    assert "read_a_guide" in run_a_script.definition.description


def test_both_hands_are_handed_over_as_one_set():
    assert GUIDE_TOOLS == [read_a_guide, run_a_script]


def test_both_hands_are_ones_she_actually_has():
    """没挂进 ``MOMENT_TOOLS`` 是静默失败：本文件全绿，而她这一轮里根本没有这两只手。"""
    from app.living.moment import MOMENT_TOOLS

    assert read_a_guide in MOMENT_TOOLS, "她手里没有读说明这只手"
    assert run_a_script in MOMENT_TOOLS, "她手里没有跑脚本这只手"


def test_the_list_variable_is_named_the_same_everywhere():
    """变量名没有编译期校验：改一个字，Langfuse 那边就原样渲染成 ``{{...}}`` 给她看。

    所以名字只在这里定义一次，moment 那侧引它，不各写一遍字面量。
    """
    from app.living import moment as moment_mod

    assert GUIDES_VAR == "guides_you_can_read"
    assert moment_mod.GUIDES_VAR is GUIDES_VAR


def test_nothing_she_reads_or_runs_survives_past_this_moment():
    """这两只手不落任何库：读回来的说明、跑出来的结果都只活在这一轮的上下文里。

    这里长出一张表就意味着有个东西替她把「读过什么」留了下来，而留什么该由她自己记
    （``keep_in_mind``）。
    """
    from app.runtime.data import Data

    tables = [
        name
        for name, obj in vars(guides_mod).items()
        if isinstance(obj, type) and issubclass(obj, Data) and obj is not Data
    ]
    assert tables == [], f"这两只手长出了库：{tables}"
