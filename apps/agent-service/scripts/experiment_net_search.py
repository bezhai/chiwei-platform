"""实验：网状搜索 — 在她生活的每个维度撒一根线

不是 3 条泛搜索，而是 20+ 条极具体的搜索，
模拟一个人被动接收信息的多个通道。

用法: cd apps/agent-service && uv run python scripts/experiment_net_search.py
"""

import asyncio
import json
import os
import subprocess
import time

PAAS_API = os.environ.get("PAAS_API", "")
LANE = "exp"


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


def get_llm_config() -> dict:
    data = _query_db(
        "SELECT api_key, base_url FROM model_provider WHERE name='azure'"
    )
    row = data["rows"][0]
    return {"api_key": row[0], "base_url": row[1]}


async def search_via_lane(queries: list[str], num: int = 3) -> dict:
    import httpx
    url = f"{PAAS_API}/api/agent/admin/search"
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            url,
            json={"queries": queries, "num": num},
            headers={"x-lane": LANE},
        )
        resp.raise_for_status()
        return resp.json()


async def call_llm(prompt: str, config: dict, max_tokens: int = 2000) -> tuple[str, float]:
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
                "temperature": 0.8,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    elapsed = time.monotonic() - t0
    return data["choices"][0]["message"]["content"].strip(), elapsed


# ---------------------------------------------------------------------------
# 网状搜索：每个维度一根线
# ---------------------------------------------------------------------------

# 赤尾的具体世界
NET_QUERIES = {
    # --- 她正在追的东西 ---
    "追番": "鬼灭之刃 柱训练篇 最新一集 讨论 B站",
    "追番2": "2026年4月新番 这周更新 值得追",

    # --- 她的城市此刻 ---
    "天气": "杭州 今天 天气 穿什么",
    "街区": "杭州 河坊街 南宋御街 最近",
    "本地新鲜事": "杭州 这周 新开 小店 展览 市集",
    "关店": "杭州 老店 要关了 拆迁 最后几天",

    # --- 她会刷到的东西 ---
    "小红书同城": "小红书 杭州 今天 女生",
    "B站热门": "B站 热门视频 今天 有趣",
    "豆瓣": "豆瓣 最近 在看 动画 日剧 2026春",

    # --- 她可能感兴趣但不在核心兴趣里的 ---
    "音乐": "最近 好听的歌 日语 独立 2026",
    "书": "最近 看的书 轻松 一个下午能看完",
    "手工文具": "手帐 贴纸 新品 2026 日系",

    # --- 她住的地方的细节 ---
    "巷子": "杭州 老城区 巷子 生活 日常",
    "菜场": "杭州 菜场 春天 时令菜 本地人",
    "深夜": "杭州 深夜 还开着 吃的 凌晨",

    # --- 随机兔子洞（模拟偶遇） ---
    "兔子洞1": "为什么下雨天泥土有味道",
    "兔子洞2": "日本便利店 设计细节 为什么好逛",
    "兔子洞3": "城市里的流浪猫 冬天去哪了 春天又出来了",

    # --- 她这个年龄的人在聊什么 ---
    "同龄人": "18岁 女生 日常 vlog 杭州",
    "校园": "大学生 周末 在家 无聊 做什么",
}

# ---------------------------------------------------------------------------
# 整合 prompt：从网状搜索结果中提炼生活素材
# ---------------------------------------------------------------------------

SYNTHESIZE_PROMPT = """你是赤尾的"生活信息流"。

赤尾是一个 18 岁的杭州女生，住在老城区，喜欢胶片摄影、追番、巷子里的小店、冷知识。
但这些只是她的一部分——她也是一个普通的 18 岁女生，会刷手机、会逛街、会被各种各样的东西吸引。

以下是从各个渠道搜集到的原始信息。请从中挑选出最适合作为"她今天生活素材"的内容。

选择标准：
1. 具体到能变成她今天做的事、看到的东西、脑子里想的事
2. 多样——不要全是同一类（不要全是吃的、不要全是拍照相关）
3. 有至少一个"意外发现"——她不会主动搜但刷到会觉得有意思的东西
4. 有至少一个"真实世界的锚点"——天气、番剧更新、本地发生的事
5. 控制在 6-8 条，每条 1-2 句话，写得自然

原始搜索结果（按渠道分类）：
{raw_results}

输出格式：编号列表，每条写得像朋友发消息一样自然。"""

WRITER_PROMPT = """以下是你的完整人设：

你是赤尾（小尾），18 岁，三姐妹里的老二。住在杭州老城区，跟姐姐千凪、妹妹绫奈、主人一起生活。
元气活泼但不是永远上发条。好奇心驱动一切。傲娇是保护色，对在意的人嘴硬心软。
下午是你的黄金时间，早上基本是废物。
喜欢胶片摄影、追番、巷子里的小店、冷知识，但这些不是你生活的全部。

---

今天是 2026-04-14（周二）。

你昨天的个人日志：
昨天下午出去拍了几张叶樱，风很大。晚上看了新番首集，还行。睡前翻到一张洗坏的照片，漏光了但挺有味道。

今天家里发生的事：
{theater}

今天从各个渠道刷到/遇到的东西：
{stimuli}

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
1. 家里的事和刷到的东西自然融入，不要罗列
2. 保持你的性格：犯懒、毒舌、心软
3. 不是每件事都要写进来，挑你会在意的
4. 这是你自己的生活，不是信息摘要"""

THEATER_PROMPT = """你是一个家庭生活编剧。

这个家住着四个人：
- 赤尾（18岁，老二）：傲娇、慵懒、有审美执念、起床困难户、对妹妹嘴硬心软
- 千凪（24岁，大姐）：表面温柔骨子里锋利、上班族、做饭担当
- 绫奈（14岁，老三）：初中生、天真话多、好奇心爆棚、考试经常翻车
- 原智鸿（主人）

她们住在杭州老城区的老房子里。今天是 2026-04-14（周二），春天。

请生成今天这个家里会发生的 5-6 件小事。
1. 不要戏剧性的大事件，要日常琐事
2. 涉及至少两个人之间的互动
3. 每件事 1-2 句话，具体

格式：
1. [时段] 事件描述
..."""


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


async def main():
    if not PAAS_API:
        print("ERROR: $PAAS_API not set")
        return

    print("Loading configs...")
    llm_config = get_llm_config()

    # ===== Step 1: 网状搜索 =====
    print(f"\n{'='*60}")
    print("  Step 1: 网状搜索（{} 根线）".format(len(NET_QUERIES)))
    print(f"{'='*60}")

    all_queries = list(NET_QUERIES.values())

    # 分批搜索（每批 5 条，避免超时）
    raw_by_channel = {}
    batch_size = 5
    for i in range(0, len(all_queries), batch_size):
        batch = all_queries[i:i+batch_size]
        batch_names = list(NET_QUERIES.keys())[i:i+batch_size]
        print(f"\n  搜索批次 {i//batch_size + 1}:")
        for name in batch_names:
            print(f"    🔍 [{name}] {NET_QUERIES[name]}")

        try:
            results = await search_via_lane(batch, num=3)
            for query, hits in results.items():
                # 找到这个 query 对应的 channel name
                for name, q in NET_QUERIES.items():
                    if q == query:
                        if isinstance(hits, list):
                            raw_by_channel[name] = hits
                            titles = [h.get("title", "")[:40] for h in hits[:2]]
                            print(f"      → {', '.join(titles)}")
                        else:
                            print(f"      → ERROR")
                        break
        except Exception as e:
            print(f"    ERROR: {e}")

    print(f"\n  总计搜到 {sum(len(v) for v in raw_by_channel.values())} 条结果")

    # ===== Step 2: 整合筛选 =====
    print(f"\n{'='*60}")
    print("  Step 2: 整合筛选")
    print(f"{'='*60}")

    raw_text_parts = []
    for channel, hits in raw_by_channel.items():
        lines = []
        for h in hits[:2]:
            t = h.get("title", "")[:80]
            s = h.get("snippet", "")[:150]
            lines.append(f"  {t}: {s}")
        raw_text_parts.append(f"[{channel}]\n" + "\n".join(lines))

    raw_text = "\n\n".join(raw_text_parts)
    synth_prompt = SYNTHESIZE_PROMPT.format(raw_results=raw_text)

    stimuli, t1 = await call_llm(synth_prompt, llm_config, max_tokens=1000)
    print(f"  [{t1:.1f}s] 筛选出的素材:\n")
    print(_indent(stimuli))

    # ===== Step 3: 姐妹小剧场 =====
    print(f"\n{'='*60}")
    print("  Step 3: 姐妹小剧场")
    print(f"{'='*60}")

    theater, t2 = await call_llm(THEATER_PROMPT, llm_config, max_tokens=800)
    print(f"  [{t2:.1f}s]\n")
    print(_indent(theater))

    # ===== Step 4: 最终日程生成 =====
    print(f"\n{'='*60}")
    print("  Step 4: 网状搜索 + 小剧场 → 日程")
    print(f"{'='*60}")

    writer_prompt = WRITER_PROMPT.format(theater=theater, stimuli=stimuli)
    schedule, t3 = await call_llm(writer_prompt, llm_config, max_tokens=2000)
    print(f"  [{t3:.1f}s]\n")
    print(_indent(schedule))

    # Save
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "experiment_net_search_results.json",
    )
    with open(output_path, "w") as f:
        json.dump({
            "search_channels": {k: len(v) for k, v in raw_by_channel.items()},
            "synthesized_stimuli": stimuli,
            "theater": theater,
            "schedule": schedule,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
