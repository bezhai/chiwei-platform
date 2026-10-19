"""Replay: the tools of her regular round (``run_moment``) that no other baseline calls.

Each scenario is one to three of her moments in which she uses one group of tools, in order. What
the outside services answer is given here (``replay.web``, ``replay.image_model``,
``replay.sandbox``, ``replay.objects.on_the_web``); everything from the tool down to the HTTP
transport runs for real.

* ``body_and_phone`` — she settles in the living room and moves to the balcony with the same
  thing in hand (``move_to``), pages through her phone (``look_through_your_phone``, first screen
  and past its end) and looks up a contact by name, one found and one not (``look_up_contact``).
* ``web`` — she searches with a question (``search_online``: search, read each page, rerank the
  pages against her question) and browses a direction (``browse_online``: one batch of results,
  no reranking). After the next hourly cleanup she searches again; the reranker scores the page
  below the relevance floor, so the tool tells her nothing lined up. What she read stays in her
  history until it is a full cleanup period old: after the cleanup an hour later both first
  results are replaced by the placeholder line, while the later search's answer is still there.
* ``pictures`` — she draws a picture (``draw_a_picture``: the image model, then tool-service
  stores it) and pages through her pictures, the new one above one from yesterday
  (``look_through_your_pictures``). Ten minutes later she finds two pictures online
  (``find_a_picture_online``: image search, then tool-service stores each one from the web).
* ``guides`` — with one guide on hand, she reads it (``read_a_guide``: its ``!`cmd``` runs in the
  sandbox first and its output is put into the text), runs the script it teaches with the guide
  named (``run_a_script``), and runs a command of her own that fails.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.infra.cst_time import CST, now_cst
from app.living.moment import run_moment
from app.living.pictures import remember_a_picture
from tests.replay import seeds
from tests.replay.harness import Reply, ToolUse
from tests.replay.harness.web import FoundImage, Hit

pytestmark = pytest.mark.integration

MOMENT = "living_life_moment"


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


async def _her_household_is_up(replay) -> None:
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    # world runs in its own process: its inbox is there, and it answers what she asks.
    replay.broker.declare_inbox(
        "world", answers=lambda question: "客厅里只有你一个人，窗外是晴天。"
    )
    await replay.start("agent-service")


def _moment(replay):
    return lambda: run_moment(lane=replay.lane, persona_id="akao", clock=now_cst)


def _stop() -> Reply:
    return Reply(tools=(ToolUse("stop_for_now", {}),))


async def test_body_and_phone(replay):
    await _her_household_is_up(replay)
    await seeds.bezhai_says("晚上一起吃饭吗？", at=_at(13, 50), name="dm-1")

    replay.model.script(
        MOMENT,
        Reply(
            tools=(
                ToolUse(
                    "switch_to",
                    {
                        "doing": "整理胶片",
                        "place": "家/客厅",
                        "because": "下午光线正好",
                    },
                ),
            ),
        ),
        Reply(tools=(ToolUse("move_to", {"place": "家/阳台"}),)),
        Reply(tools=(ToolUse("look_through_your_phone", {}),)),
        Reply(
            tools=(
                ToolUse(
                    "look_through_your_phone", {"before": str(seeds.DM_WITH_BEZHAI)}
                ),
            ),
        ),
        Reply(
            tools=(
                ToolUse("look_up_contact", {"name": "bezhai"}),
                ToolUse("look_up_contact", {"name": "千凪"}),
            ),
        ),
        _stop(),
    )
    await replay.step(
        "she moves to the balcony and goes through her phone",
        _moment(replay),
        at=_at(14, 0),
    )

    replay.check("living_tools/body_and_phone")


RAIN_QUESTION = "明天东京会下雨吗"
RAIN_PAGES = {
    "https://example.com/tokyo/forecast": (
        "# 东京都 明日天气\n\n"
        "7 月 26 日（周日）阴转小雨，降水概率 70%，最高 29 度，最低 24 度。\n"
        "午后起东京湾沿岸有阵雨，傍晚雨势减弱。"
    ),
    "https://example.com/news/rainy-season": (
        "# 关东地区梅雨季进入尾声\n\n"
        "气象厅表示，关东甲信地区预计在本周内宣布出梅，之后以晴热天气为主。"
    ),
}
NEW_ANIME = "最近有什么好看的新番"
FIREWORKS_QUESTION = "隅田川花火大会今年是哪天"
FIREWORKS_PAGE = (
    "https://example.com/blog/summer",
    "# 夏天去哪玩\n\n我列了几家适合拍照的咖啡店，还有一份防晒霜测评。",
)


async def test_web(replay):
    await _her_household_is_up(replay)

    replay.web.results(
        RAIN_QUESTION,
        [
            Hit(
                "东京都 明日天气预报",
                "https://example.com/tokyo/forecast",
                "明天阴转小雨，降水概率 70%",
            ),
            Hit(
                "关东地区梅雨季进入尾声",
                "https://example.com/news/rainy-season",
                "预计本周内出梅",
            ),
        ],
    )
    for url, text in RAIN_PAGES.items():
        replay.web.page(url, text)
    replay.web.rerank(RAIN_QUESTION, [0.91, 0.38])
    replay.web.results(
        NEW_ANIME,
        [
            Hit(
                "七月新番导视：本季值得追的十部",
                "https://example.com/anime/july",
                "从日常到科幻",
            ),
            Hit(
                "《孤独摇滚！》第二季制作决定", "https://example.com/anime/bocchi-2", ""
            ),
            Hit("", "https://example.com/anime/untitled", "一条没有标题的帖子"),
        ],
    )
    replay.model.script(
        MOMENT,
        Reply(tools=(ToolUse("search_online", {"question": RAIN_QUESTION}),)),
        Reply(tools=(ToolUse("browse_online", {"direction": NEW_ANIME}),)),
        _stop(),
    )
    await replay.step("she searches and browses", _moment(replay), at=_at(14, 0))

    replay.web.results(
        FIREWORKS_QUESTION,
        [Hit("夏天去哪玩", FIREWORKS_PAGE[0], "咖啡店和防晒霜")],
    )
    replay.web.page(*FIREWORKS_PAGE)
    replay.web.rerank(FIREWORKS_QUESTION, [0.04])
    replay.model.script(
        MOMENT,
        Reply(tools=(ToolUse("search_online", {"question": FIREWORKS_QUESTION}),)),
        Reply(text="没查到对得上的，回头问问绫奈。"),
    )
    await replay.step(
        "after the next cleanup she searches again; nothing lines up",
        _moment(replay),
        at=_at(15, 10),
    )

    replay.model.script(MOMENT, Reply(text="出门前再看一眼天。"))
    await replay.step(
        "a cleanup later, what she read first is replaced by the placeholder",
        _moment(replay),
        at=_at(16, 10),
    )

    replay.check("living_tools/web")


DRAWING = "窗台上一杯抹茶拿铁，午后的阳光斜照进来"
FINDING = "孤独摇滚 后藤一里 剧照"
FOUND = (
    FoundImage(
        "https://example.com/img/bocchi-guitar.png",
        "后藤一里抱着吉他",
        "https://example.com/bocchi/1",
    ),
    FoundImage(
        "https://example.com/img/bocchi-box.png", "", "https://example.com/bocchi/2"
    ),
)


async def test_pictures(replay):
    await _her_household_is_up(replay)
    replay.objects.put(
        "pictures/akao-sakura.png", seeds.png(250, 200, 220), "image/png"
    )
    await remember_a_picture(
        lane=replay.lane,
        persona_id="akao",
        file_name="pictures/akao-sakura.png",
        what="窗外的樱花",
        made_at=datetime(2026, 7, 24, 20, 0, tzinfo=CST),
    )

    replay.image_model.draws(DRAWING, seeds.png(120, 180, 90))
    replay.model.script(
        MOMENT,
        Reply(tools=(ToolUse("draw_a_picture", {"what": DRAWING}),)),
        Reply(tools=(ToolUse("look_through_your_pictures", {}),)),
        _stop(),
    )
    await replay.step(
        "she draws a picture and goes through hers", _moment(replay), at=_at(14, 0)
    )

    replay.web.images(FINDING, list(FOUND))
    replay.objects.on_the_web(FOUND[0].image_url, seeds.png(240, 160, 180), "image/png")
    replay.objects.on_the_web(FOUND[1].image_url, seeds.png(60, 60, 200), "image/png")
    replay.model.script(
        MOMENT,
        Reply(tools=(ToolUse("find_a_picture_online", {"what": FINDING}),)),
        _stop(),
    )
    await replay.step("she finds pictures online", _moment(replay), at=_at(14, 10))

    replay.check("living_tools/pictures")


GUIDE = """---
name: bangumi
description: 查番剧条目：放送时间、集数、制作公司
---
# 查番剧条目

脚本在 $SKILL_DIR/scripts/bangumi.py。它现在支持的用法：

!`python3 $SKILL_DIR/scripts/bangumi.py --help`

查一部番就把名字交给 search，结果里有放送时间、集数和制作公司。
"""
GUIDE_HELP = "python3 $SKILL_DIR/scripts/bangumi.py --help"
LOOK_UP = "python3 /sandbox/skills/bangumi/scripts/bangumi.py search 孤独摇滚"
OWN_SUM = 'python3 -c "print(sum(range(1, 11)) / 0)"'


async def test_guides(replay):
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox("world")
    replay.guide("bangumi", GUIDE)
    await replay.start("agent-service")

    replay.sandbox.runs(
        GUIDE_HELP,
        stdout="usage: bangumi.py search <名字>\n  按名字查条目，打印放送时间、集数、制作公司\n",
    )
    replay.sandbox.runs(
        LOOK_UP,
        stdout="孤独摇滚！ | 2022-10-09 开播 | 12 话 | CloverWorks\n",
    )
    replay.sandbox.runs(
        OWN_SUM,
        stderr=(
            "Traceback (most recent call last):\n"
            '  File "<string>", line 1, in <module>\n'
            "ZeroDivisionError: division by zero\n"
        ),
        exit_code=1,
    )
    replay.model.script(
        MOMENT,
        Reply(tools=(ToolUse("read_a_guide", {"which": "bangumi"}),)),
        Reply(
            tools=(ToolUse("run_a_script", {"command": LOOK_UP, "guide": "bangumi"}),)
        ),
        Reply(tools=(ToolUse("run_a_script", {"command": OWN_SUM}),)),
        _stop(),
    )
    await replay.step(
        "she reads the guide and runs its script", _moment(replay), at=_at(14, 0)
    )

    replay.check("living_tools/guides")
