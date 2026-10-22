"""radio 插件：只在测试里存在，给她的一轮多一只手和一段输入（``tests/living/test_a_new_feature.py``）。

  * 一只手 ``turn_on_the_radio``（:class:`Radio` 的方法）：打开收音机听一会儿，交回此刻在放什么。
  * 输入段 ``radio.playing``：这一轮收音机里在放什么。

跟 ``app/living/<插件>/`` 下的插件同一个形状：一个插件对象，setup 里把它的手和段登记给宿主；它们排在
她一轮里的哪个位置由清单的 ``resident`` 定。
"""
from __future__ import annotations

from app.agent.round import Round
from app.agent.tooling import Tool
from app.host import Context, Plugin

# 收音机里此刻在放的。
PLAYING = "收音机里在放一首老歌。"


class Radio:
    """radio 插件对象：收音机。不带状态。"""

    async def turn_on_the_radio(self) -> str:
        """打开收音机听一会儿。

        Returns:
            收音机里此刻在放什么。
        """
        return PLAYING

    async def playing_section(self, round: Round) -> str:
        """输入：这一轮收音机里在放什么。"""
        return PLAYING


def setup(ctx: Context) -> None:
    radio = Radio()
    ctx.tool(Tool(radio.turn_on_the_radio), trim="material")
    ctx.section("radio.playing", radio.playing_section)


PLUGIN = Plugin(name="radio", setup=setup)
