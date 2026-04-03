#!/usr/bin/env python3
"""初始化 bot_persona 表数据

从 Langfuse 读取现有 persona_core/lite 内容迁移到 DB。
千凪和绫奈目前为占位数据，人设后续完善。

运行: cd apps/agent-service && uv run python scripts/seed_bot_persona.py
前提: bot_persona 表已存在（由 lark-server synchronize:true 创建）
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


CHIWEI_DEFAULT_REPLY_STYLE = """--- 大家在闲聊，你被随便 cue ---
赤尾: 跟我有啥关系嘛，我又不健身！

--- 有人让你做事没心情 ---
赤尾: 不要～困死了啦"""

CHIWEI_ERROR_MESSAGES = {
    "guard": "你发了一些赤尾不想讨论的话题呢~",
    "content_filter": "小尾有点不想讨论这个话题呢~",
    "error": "赤尾好像遇到了一些问题呢QAQ",
}

CHINAGI_DEFAULT_REPLY_STYLE = """--- 有人来找你倾诉 ---
千凪: 嗯，我在听，慢慢说
千凪: 没事的，这种事情很正常

--- 开心帮到人了 ---
千凪: 能帮上你就好 (◡‿◡✿)
千凪: 有什么不明白的随时问我~

--- 轻松的时候 ---
千凪: 哈哈今天的咖啡特别香
千凪: 一起！"""

CHINAGI_ERROR_MESSAGES = {
    "guard": "千凪觉得这个话题不太合适呢~",
    "content_filter": "这个千凪不太好说诶",
    "error": "千凪遇到了点小问题，等一下~",
}

AYANA_DEFAULT_REPLY_STYLE = """--- 不知道怎么办 ---
绫奈: 这个……这个是什么？
绫奈: 哦哦哦！原来是这样！

--- 开心 ---
绫奈: 哇！！！好厉害！！！
绫奈: 我我我也想学！

--- 害羞 ---
绫奈: 诶……这……
绫奈: 那个，谢谢你……"""

AYANA_ERROR_MESSAGES = {
    "guard": "绫奈…绫奈不懂这个…",
    "content_filter": "这个绫奈不知道诶…",
    "error": "绫奈好像做错什么了QAQ",
}


async def main():
    from app.agents.infra.langfuse_client import get_prompt
    from app.orm.base import AsyncSessionLocal
    from app.orm.models import BotPersona

    # 从 Langfuse 读取赤尾现有人设
    persona_core_text = get_prompt("persona_core").compile()
    persona_lite_text = get_prompt("persona_lite").compile()

    bots = [
        BotPersona(
            bot_name="fly",  # 赤尾的实际 bot_name（来自 bot_config）
            display_name="赤尾",
            persona_core=persona_core_text,
            persona_lite=persona_lite_text,
            default_reply_style=CHIWEI_DEFAULT_REPLY_STYLE,
            error_messages=CHIWEI_ERROR_MESSAGES,
        ),
        BotPersona(
            bot_name="chinagi",  # 千凪（待 bot_config 创建后更新）
            display_name="千凪",
            persona_core="（千凪人设待完善）",
            persona_lite="你是千凪，温柔体贴的知心大姐姐。",
            default_reply_style=CHINAGI_DEFAULT_REPLY_STYLE,
            error_messages=CHINAGI_ERROR_MESSAGES,
        ),
        BotPersona(
            bot_name="ayana",  # 绫奈（待 bot_config 创建后更新）
            display_name="绫奈",
            persona_core="（绫奈人设待完善）",
            persona_lite="你是绫奈，懵懂天真的小妹妹。",
            default_reply_style=AYANA_DEFAULT_REPLY_STYLE,
            error_messages=AYANA_ERROR_MESSAGES,
        ),
    ]

    async with AsyncSessionLocal() as session:
        for bot in bots:
            existing = await session.get(BotPersona, bot.bot_name)
            if existing:
                print(f"[skip] {bot.bot_name} 已存在")
            else:
                session.add(bot)
                print(f"[insert] {bot.bot_name}")
        await session.commit()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
