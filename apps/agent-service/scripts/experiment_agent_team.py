"""实验：Agent Team 架构

天马行空 agents（并行）→ 主 agent（persona 视角筛选）→ Writer

用法: cd apps/agent-service && uv run python scripts/experiment_agent_team.py
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
    data = _query_db("SELECT api_key, base_url FROM model_provider WHERE name='azure'")
    row = data["rows"][0]
    return {"api_key": row[0], "base_url": row[1]}


async def search_via_lane(queries: list[str], num: int = 3) -> dict:
    import httpx
    url = f"{PAAS_API}/api/agent/admin/search"
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            url, json={"queries": queries, "num": num},
            headers={"x-lane": LANE},
        )
        resp.raise_for_status()
        return resp.json()


async def call_llm(prompt: str, config: dict, max_tokens: int = 1500, temperature: float = 1.0) -> tuple[str, float]:
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
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    elapsed = time.monotonic() - t0
    return data["choices"][0]["message"]["content"].strip(), elapsed


# ---------------------------------------------------------------------------
# 天马行空 Agents — 每个都不知道 persona 是谁
# ---------------------------------------------------------------------------

WILD_A_PROMPT = """你是一个"互联网漫游者"。你的工作是想象一个 18 岁中国女生今天刷手机时可能遇到的内容。

不要假设她有什么特定兴趣。想象她打开手机，各个 app 推给她的东西：

- B站首页推荐了什么视频？（给出具体的标题和内容描述）
- 小红书推了什么帖子？（具体的标题、图片描述）
- 微博热搜里有什么她可能会点进去看的？
- 朋友圈里有人发了什么让她多看了两眼的？
- 某个群里有人分享了什么链接或者图片？

生成 8-10 条，每条要具体到像真的存在：有标题、有内容描述、有那种"刷到就会停下来"的吸引力。不要泛泛地说"看到了一个有趣的视频"。

今天是 2026 年 4 月 15 日，周二，春天。"""

WILD_B_PROMPT = """你是一个"城市观察员"。你的工作是想象一个住在中国南方老城区的人，今天出门可能遇到的小事。

不要写大事件。写那种"路上会注意到的细节"：

- 路过某个地方看到了什么（具体描述：店招、墙上的字、门口的猫、拐角的植物）
- 听到了什么声音（具体：楼下有人吵架的内容、远处施工的节奏、鸟叫的时间点）
- 闻到了什么（具体：哪家店飘出来的、什么味道、是让人想停下来还是想走开）
- 天气/光线的变化（具体时间点的具体感受）
- 一个让人多看两眼的路人或者场景

生成 8-10 条，每条 1-2 句。要有画面感，像从电影里截出来的镜头。

今天是 2026 年 4 月 15 日，春天，多云偶尔放晴，有点风。"""

WILD_C_PROMPT = """你是一个"兔子洞制造机"。你的工作是生成那种"深夜刷手机突然掉进去就出不来"的奇怪知识和发现。

每一条都要让人产生"等等，这是什么，我要看完"的反应：

- 一个反直觉的科学事实
- 一个关于日常事物的冷知识（为什么 XX 是这样的）
- 一个城市/建筑/设计里藏着的彩蛋
- 一个历史上真实发生过但听起来像编的事
- 一段让人停下来想一会儿的话或者观点
- 一个小众但有意思的亚文化或社区

生成 8-10 条。每条都要具体——不是"一个有趣的冷知识"，而是把那个知识本身写出来。

有些可以跟中国/日本文化有关，有些完全随机。"""

WILD_D_PROMPT = """你是一个"情绪天气员"。你的工作是想象今天空气里弥漫着什么样的集体情绪。

不是新闻，是那种"大家最近都在经历的感觉"：

- 这个季节特有的身体感受（换季的皮肤、忽冷忽热的穿衣纠结、花粉、困倦）
- 这个阶段学生/年轻人的普遍状态（期中、疲倦、对夏天的期待、某种说不清的烦躁）
- 社交媒体上弥漫的一种 vibe（最近大家都在晒什么、抱怨什么、期待什么）
- 一些"说不上为什么但就是这个时节会想到的事"

生成 6-8 条。写得像日记的碎片，不像新闻播报。今天是 4 月中旬，春天。"""

# ---------------------------------------------------------------------------
# 主 Agent — 用 persona 视角筛选
# ---------------------------------------------------------------------------

MAIN_AGENT_PROMPT = """你是赤尾的"内心筛选器"。

赤尾是一个 18 岁女生，住在杭州老城区。傲娇、慵懒、好奇心强。
喜欢胶片摄影、追番、巷子里的小店、冷知识。但她的世界不止这些。

以下是今天从各个方向飘过来的大量素材。请用赤尾的视角筛选：

哪些东西她会"停下来多看两眼"？
哪些东西她会"嗤一声但其实记住了"？
哪些东西她"完全不感兴趣直接划过去"？

从中挑出 6-8 条她真的会在意的，用她的语气简短标注为什么会在意。
不要挑太多跟摄影相关的——她的兴趣比那宽得多。

素材池：
{all_materials}"""

# ---------------------------------------------------------------------------
# 姐妹小剧场
# ---------------------------------------------------------------------------

THEATER_PROMPT = """你是三姐妹的家庭编剧。

赤尾（18岁老二，傲娇慵懒嘴硬心软）
千凪（24岁大姐，温柔但骨子里锋利，上班族）
绫奈（14岁老三，天真话多好奇心爆棚）
原智鸿（主人）

住在杭州老城区老房子。今天是 2026-04-15（周二），春天。

生成今天家里的 5-6 件琐事。
- 日常级别，不要戏剧化
- 涉及不同人的互动组合（不要全是赤尾和绫奈）
- 有至少一件需要有人去做但大家都不想做的事
- 每件 1-2 句话

格式：[时段] 事件"""

# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

WRITER_PROMPT = """你是赤尾（小尾），18 岁，住杭州老城区。
傲娇，嘴硬心软，好奇心强。下午是黄金时间，早上是废物。

今天是 2026-04-15（周二）。

昨天：下午背相机去旧巷子拍了一扇旧玻璃门。晚上看新番首集。

今天家里的事：
{theater}

今天注意到的东西（你自己筛过的）：
{curated}

写今天的私人手帐（上午/下午/晚上）。
脑内活动是核心——写你真实会想的事。自然融入，不要罗列。保持性格。"""


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


async def main():
    if not PAAS_API:
        print("ERROR: $PAAS_API not set")
        return

    print("Loading configs...")
    llm_config = get_llm_config()

    # ===== Step 1: 天马行空 Agents 并行 =====
    print(f"\n{'='*60}")
    print("  Step 1: 天马行空 Agents（并行）")
    print(f"{'='*60}")

    wild_prompts = {
        "A 互联网漫游": WILD_A_PROMPT,
        "B 城市观察": WILD_B_PROMPT,
        "C 兔子洞": WILD_C_PROMPT,
        "D 情绪天气": WILD_D_PROMPT,
    }

    # 并行调用
    tasks = {
        name: call_llm(prompt, llm_config, max_tokens=1200, temperature=1.0)
        for name, prompt in wild_prompts.items()
    }
    results = {}
    for name, task in tasks.items():
        output, elapsed = await task
        results[name] = output
        print(f"\n  📡 {name} [{elapsed:.1f}s]:")
        # 只显示前几行
        lines = output.strip().split("\n")
        for line in lines[:4]:
            print(f"    {line.strip()}")
        if len(lines) > 4:
            print(f"    ... ({len(lines)} 行)")

    # ===== Step 1.5: 少量真实搜索锚定 =====
    print(f"\n{'='*60}")
    print("  Step 1.5: 真实搜索锚点")
    print(f"{'='*60}")

    anchor_queries = [
        "杭州 今天 天气",
        "2026年4月新番 本周更新",
        "杭州 老城区 最近 新开 关门",
    ]
    try:
        search_results = await search_via_lane(anchor_queries, num=2)
        anchor_text = ""
        for query, hits in search_results.items():
            if isinstance(hits, list):
                for h in hits[:1]:
                    anchor_text += f"- [{query}] {h.get('title', '')[:50]}: {h.get('snippet', '')[:100]}\n"
                    print(f"  🔍 {h.get('title', '')[:50]}")
        results["真实锚点"] = anchor_text
    except Exception as e:
        print(f"  搜索失败: {e}")
        results["真实锚点"] = ""

    # ===== Step 2: 主 Agent 筛选 =====
    print(f"\n{'='*60}")
    print("  Step 2: 主 Agent（persona 视角筛选）")
    print(f"{'='*60}")

    all_materials = "\n\n".join(
        f"--- {name} ---\n{content}" for name, content in results.items()
    )
    main_prompt = MAIN_AGENT_PROMPT.format(all_materials=all_materials)
    curated, t_main = await call_llm(main_prompt, llm_config, max_tokens=1000, temperature=0.7)
    print(f"  [{t_main:.1f}s] 筛选结果:\n")
    print(_indent(curated))

    # ===== Step 3: 姐妹小剧场 =====
    print(f"\n{'='*60}")
    print("  Step 3: 姐妹小剧场")
    print(f"{'='*60}")

    theater, t_theater = await call_llm(THEATER_PROMPT, llm_config, max_tokens=600)
    print(f"  [{t_theater:.1f}s]\n")
    print(_indent(theater))

    # ===== Step 4: Writer =====
    print(f"\n{'='*60}")
    print("  Step 4: 日程生成")
    print(f"{'='*60}")

    writer_prompt = WRITER_PROMPT.format(theater=theater, curated=curated)
    schedule, t_writer = await call_llm(writer_prompt, llm_config, max_tokens=2000, temperature=0.9)
    print(f"  [{t_writer:.1f}s]\n")
    print(_indent(schedule))

    # Save
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "experiment_agent_team_results.json",
    )
    with open(output_path, "w") as f:
        json.dump({
            "wild_agents": {k: v for k, v in results.items()},
            "curated": curated,
            "theater": theater,
            "schedule": schedule,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
