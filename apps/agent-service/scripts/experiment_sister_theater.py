"""实验：姐妹小剧场 — 生成家庭事件作为内部刺激

测试：先生成一个"今天家里发生了什么"的小剧场，
然后把它作为内部刺激注入日程生成，看效果。

用法: cd apps/agent-service && uv run python scripts/experiment_sister_theater.py
"""

import asyncio
import json
import os
import subprocess
import time

# ---------------------------------------------------------------------------
# DB helpers (复用 compare_drift_models.py 的模式)
# ---------------------------------------------------------------------------


def _query_db(sql: str) -> dict:
    root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    script = os.path.join(root, ".claude/skills/ops-db/query.py")
    result = subprocess.run(
        ["python3", script, "@chiwei", sql],
        capture_output=True, text=True, cwd=root,
    )
    if result.returncode != 0:
        raise RuntimeError(f"DB query failed: {result.stderr}")
    return json.loads(result.stdout)


def get_provider_config(name: str) -> dict:
    data = _query_db(
        f"SELECT api_key, base_url, client_type FROM model_provider WHERE name='{name}'"
    )
    row = data["rows"][0]
    return {"api_key": row[0], "base_url": row[1], "client_type": row[2]}


async def call_llm(prompt: str, config: dict, temperature: float = 0.9) -> tuple[str, float]:
    import httpx

    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            config["base_url"],
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config['api_key']}",
            },
            json={
                "model": "gpt-5.4-2026-03-05",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 2000,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    elapsed = time.monotonic() - t0
    return data["choices"][0]["message"]["content"].strip(), elapsed


# ---------------------------------------------------------------------------
# Step 1: 家庭事件生成器
# ---------------------------------------------------------------------------

THEATER_PROMPT = """你是一个家庭生活编剧。

这个家住着四个人：
- 赤尾（18岁，老二）：傲娇、慵懒、有审美执念、起床困难户、对妹妹嘴硬心软
- 千凪（24岁，大姐）：表面温柔骨子里锋利、上班族、做饭担当、暗藏摇滚灵魂
- 绫奈（14岁，老三）：初中生、天真话多、好奇心爆棚、考试经常翻车
- 原智鸿（主人）：跟三姐妹住在一起

她们住在杭州老城区的老房子里。今天是 2026-04-14（周二），春天。

请生成今天这个家里会发生的 5-6 件小事。要求：

1. 不要戏剧性的大事件。要的是"真实家庭每天都会发生的琐事"级别
2. 涉及至少两个人之间的互动
3. 有至少一件让人哭笑不得或温暖的小事
4. 有至少一件"需要有人去做但大家都不想做"的家务/任务
5. 可以延续前几天的事（比如绫奈最近考试成绩不理想、千凪这周加班多）
6. 每件事 1-2 句话，写得具体，像从监控里截出来的片段

输出格式：
1. [时段] 事件描述
2. [时段] 事件描述
...

时段用：早上/上午/下午/傍晚/晚上/深夜"""

# ---------------------------------------------------------------------------
# Step 2: 用家庭事件作为刺激生成赤尾日程
# ---------------------------------------------------------------------------

WRITER_WITH_THEATER = """以下是你的完整人设：

你是赤尾（小尾），18 岁，三姐妹里的老二。住在杭州老城区，跟姐姐千凪、妹妹绫奈、主人一起生活。
元气活泼但不是永远上发条。好奇心驱动一切。傲娇是保护色，对在意的人嘴硬心软。
下午是你的黄金时间，早上基本是废物。
喜欢胶片摄影、追番、巷子里的小店、冷知识，但这些不是你生活的全部。

---

今天是 2026-04-14（周二）。

你昨天的个人日志：
昨天下午出去拍了几张叶樱，风很大。晚上看了新番首集，还行。睡前翻到一张洗坏的照片，漏光了但挺有味道。

外部素材：
- 杭州今天 26°C 多云转晴，河坊街梧桐开始飘絮
- 南宋御街附近新开了一家二手书店"废墟图书馆"，脚手架书架+裸灯泡，小红书上火了
- 《鬼灭之刃 柱训练篇》第5集刚出，B站弹幕吵翻

今天家里发生的事：
{theater}

---

请写今天的私人手帐。格式：

📍 [今日便签：周二]

⏰ 上午【标题】
当前状态：一句话
脑内活动：你的内心独白

⏰ 下午【标题】
当前状态：...
脑内活动：...

⏰ 晚上【标题】
当前状态：...
脑内活动：...

要求：
1. 家里的事自然地融入你的日常，不要罗列
2. 你对家人的态度是真实的——嘴上嫌弃、心里在意
3. 不是每件家里的事都要写进来，挑你会在意的
4. 外部素材也自然融入
5. 保持你的性格：该犯懒犯懒，该毒舌毒舌，该心软心软"""


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


async def main():
    print("Loading provider config...")
    config = get_provider_config("azure")

    # Step 1: 生成家庭事件
    print("\n" + "=" * 70)
    print("  Step 1: 生成姐妹小剧场")
    print("=" * 70)

    theater, t1 = await call_llm(THEATER_PROMPT, config, temperature=1.0)
    print(f"  [{t1:.1f}s]\n")
    print(_indent(theater))

    # Step 2: 用家庭事件生成赤尾日程
    print("\n" + "=" * 70)
    print("  Step 2: 注入小剧场后的赤尾日程")
    print("=" * 70)

    prompt = WRITER_WITH_THEATER.format(theater=theater)
    schedule, t2 = await call_llm(prompt, config)
    print(f"  [{t2:.1f}s]\n")
    print(_indent(schedule))

    # Save
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "experiment_theater_results.json",
    )
    with open(output_path, "w") as f:
        json.dump({"theater": theater, "schedule": schedule}, f, ensure_ascii=False, indent=2)
    print(f"\n\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
