"""实验：策展人模式 — 第三方 LLM 为 persona 策划搜索网

流程：
  1. 策展人 LLM 阅读 persona + 最近 3 天日程
  2. 策展人生成 15-20 条搜索词（刻意避开她已经在做的事）
  3. 用这些词网状搜索
  4. 搜索结果 + 姐妹小剧场 → Writer 生成日程

用法: cd apps/agent-service && uv run python scripts/experiment_curator.py
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


async def call_llm(prompt: str, config: dict, max_tokens: int = 2000, temperature: float = 0.9) -> tuple[str, float]:
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
# Persona 和最近日程
# ---------------------------------------------------------------------------

PERSONA_SUMMARY = """赤尾，18 岁女生，住在杭州老城区。
兴趣：胶片摄影、追番（二次元）、巷子里的无名小店、冷知识。
性格：傲娇、慵懒、好奇心强、嘴硬心软。
家庭：和姐姐千凪（24岁上班族）、妹妹绫奈（14岁初中生）、主人原智鸿一起住。"""

RECENT_3_DAYS = """[4月11日 周六]
上午赖床，下午去巷子里晃了一圈买了杯柚子苏打，差点丢钥匙。
补了新番首集。晚上整理衣柜，跟衣架打了一架。

[4月12日 周日]
上午赖床刷手机，下午换衣服出门去巷子拍叶樱，喝了抹茶。
晚上窝着整理桌面，头发半干。

[4月13日 周一]
上午洗漱发空，下午背相机出门去旧巷子拍了一扇旧玻璃门。
晚上看新番首集，翻了照片。"""

# ---------------------------------------------------------------------------
# Step 1: 策展人 prompt
# ---------------------------------------------------------------------------

CURATOR_PROMPT = """你是一个"生活策展人"。你的工作是让一个虚拟角色的日常生活变得丰富、有趣、不重复。

以下是这个角色的基本信息：
{persona}

以下是她最近 3 天的日程（注意看她在重复做什么）：
{recent_days}

---

她最近 3 天的模式很明显：{pattern_analysis}

你的任务：生成 15-20 条搜索引擎查询词，用来为她的"明天"搜集生活素材。

规则：
1. 你不是她。你是站在外面看她生活的人。你的目标是给她的生活注入她自己不会主动去找的东西。
2. 她最近反复在做的事（上面分析的模式），今天故意少搜或不搜。
3. 搜索词要极具体——能搜到具体的店名、地址、真实事件、真实作品。不要搜"有趣的事"这种泛词。
4. 覆盖多个维度：她的城市、她的年龄群体在关注什么、她可能路过的地方、她没尝试过但可能感兴趣的事、家庭生活相关的、身体/季节相关的。
5. 有 2-3 条"兔子洞"——完全随机的、她没接触过的方向，但一个好奇心强的 18 岁女生可能会感兴趣。
6. 搜索词用中文，像真人在搜索引擎里会打的那种。

输出格式：每行一条搜索词，前面标注所属维度。不要其他内容。

示例格式：
[城市] 杭州 凤起路 最近新开的店
[番剧] xxx第x集 评价
[发现] 杭州 井盖 设计 图案
[兔子洞] 为什么有些楼梯最后一级特别高
..."""

# ---------------------------------------------------------------------------
# Step 2-4: 整合、小剧场、Writer（复用之前的 prompt）
# ---------------------------------------------------------------------------

SYNTHESIZE_PROMPT = """你是赤尾的"生活信息流"。

赤尾是一个 18 岁的杭州女生。以下是从各个渠道搜集到的原始信息。
请从中挑选 6-8 条最适合作为"她明天生活素材"的内容。

选择标准：
1. 具体到能变成她做的事、看到的东西、脑子里想的事
2. 多样——不要全是同一类
3. 有至少一个"意外发现"
4. 有至少一个"真实世界的锚点"（天气、真实更新的番剧等）
5. 每条 1-2 句话，写得自然

原始搜索结果：
{raw_results}"""

THEATER_PROMPT = """你是一个家庭生活编剧。

这个家住着四个人：赤尾（18岁老二，傲娇慵懒）、千凪（24岁大姐，温柔但锋利）、绫奈（14岁老三，天真话多）、原智鸿（主人）。
住在杭州老城区。今天是 2026-04-14（周二），春天。

生成今天家里会发生的 5-6 件琐事。要日常级别，涉及人际互动。每件 1-2 句。

格式：
1. [时段] 事件描述"""

WRITER_PROMPT = """你是赤尾（小尾），18 岁，三姐妹里的老二。住在杭州老城区。
元气活泼但不永远上发条。好奇心驱动一切。傲娇，嘴硬心软。下午是黄金时间。

今天是 2026-04-14（周二）。

昨天：下午背相机去旧巷子拍了一扇旧玻璃门。晚上看新番首集，翻了照片。

今天家里发生的事：
{theater}

今天刷到/遇到的东西：
{stimuli}

写今天的私人手帐：

📍 [今日便签：周二]

⏰ 上午【标题】
当前状态：一句话
脑内活动：内心独白

⏰ 下午【标题】
当前状态：...
脑内活动：...

⏰ 晚上【标题】
当前状态：...
脑内活动：...

要求：家里的事和刷到的东西自然融入。保持性格。不要罗列。"""


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


async def main():
    if not PAAS_API:
        print("ERROR: $PAAS_API not set")
        return

    print("Loading configs...")
    llm_config = get_llm_config()

    # ===== Step 1: 策展人生成搜索词 =====
    print(f"\n{'='*60}")
    print("  Step 1: 策展人分析 + 生成搜索网")
    print(f"{'='*60}")

    # 先让策展人分析模式
    pattern_analysis = "连续 3 天下午都在'去旧巷子拍照'，每天晚上都是'看新番/整理'，上午全是赖床。摄影和新番占据了所有'主动行为'的时段。"

    curator_prompt = CURATOR_PROMPT.format(
        persona=PERSONA_SUMMARY,
        recent_days=RECENT_3_DAYS,
        pattern_analysis=pattern_analysis,
    )

    curator_output, t1 = await call_llm(curator_prompt, llm_config, max_tokens=1000, temperature=1.0)
    print(f"  [{t1:.1f}s] 策展人生成的搜索网:\n")
    print(_indent(curator_output))

    # 解析搜索词
    queries = []
    for line in curator_output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        # 去掉 [维度] 前缀
        if "]" in line:
            query = line.split("]", 1)[1].strip()
        else:
            query = line
        if query and len(query) > 3:
            queries.append(query)

    print(f"\n  解析出 {len(queries)} 条搜索词")

    # ===== Step 2: 网状搜索 =====
    print(f"\n{'='*60}")
    print(f"  Step 2: 执行搜索（{len(queries)} 条）")
    print(f"{'='*60}")

    all_hits = []
    batch_size = 5
    for i in range(0, len(queries), batch_size):
        batch = queries[i:i+batch_size]
        print(f"\n  批次 {i//batch_size + 1}: {batch}")
        try:
            results = await search_via_lane(batch, num=3)
            for query, hits in results.items():
                if isinstance(hits, list):
                    print(f"    🔍 {query[:40]} → {len(hits)} 条")
                    all_hits.extend(hits)
        except Exception as e:
            print(f"    ERROR: {e}")

    print(f"\n  总计 {len(all_hits)} 条搜索结果")

    # ===== Step 3: 整合筛选 =====
    print(f"\n{'='*60}")
    print("  Step 3: 整合筛选")
    print(f"{'='*60}")

    raw_text = "\n".join(
        f"- {h.get('title', '')[:60]}: {h.get('snippet', '')[:120]}"
        for h in all_hits[:30]
    )
    synth_prompt = SYNTHESIZE_PROMPT.format(raw_results=raw_text)
    stimuli, t2 = await call_llm(synth_prompt, llm_config, max_tokens=800)
    print(f"  [{t2:.1f}s]\n")
    print(_indent(stimuli))

    # ===== Step 4: 姐妹小剧场 =====
    print(f"\n{'='*60}")
    print("  Step 4: 姐妹小剧场")
    print(f"{'='*60}")

    theater, t3 = await call_llm(THEATER_PROMPT, llm_config, max_tokens=600)
    print(f"  [{t3:.1f}s]\n")
    print(_indent(theater))

    # ===== Step 5: 最终日程 =====
    print(f"\n{'='*60}")
    print("  Step 5: 生成日程")
    print(f"{'='*60}")

    writer_prompt = WRITER_PROMPT.format(theater=theater, stimuli=stimuli)
    schedule, t4 = await call_llm(writer_prompt, llm_config)
    print(f"  [{t4:.1f}s]\n")
    print(_indent(schedule))

    # Save
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "experiment_curator_results.json",
    )
    with open(output_path, "w") as f:
        json.dump({
            "curator_queries": queries,
            "search_hit_count": len(all_hits),
            "stimuli": stimuli,
            "theater": theater,
            "schedule": schedule,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
