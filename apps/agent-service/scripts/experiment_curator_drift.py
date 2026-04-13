"""实验：策展人漂移测试 — 连续 5 天，看搜索词是否趋同

模拟连续 5 天运行策展人：
  Day 1: 用真实的最近 3 天（全是拍照）
  Day 2: 用 Day 1 生成的日程替换掉最早一天
  Day 3-5: 滚动窗口继续

观察：
  - 每天的搜索词是否多样？
  - 跨天之间是否出现重复模式？
  - 策展人是否真的在"避开"前几天的内容？

用法: cd apps/agent-service && uv run python scripts/experiment_curator_drift.py
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
            url, json={"queries": queries, "num": num},
            headers={"x-lane": LANE},
        )
        resp.raise_for_status()
        return resp.json()


async def call_llm(prompt: str, config: dict, max_tokens: int = 1500, temperature: float = 0.9) -> tuple[str, float]:
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
# Prompts
# ---------------------------------------------------------------------------

PERSONA_SUMMARY = """赤尾，18 岁女生，住在杭州老城区。
兴趣：胶片摄影、追番（二次元）、巷子里的无名小店、冷知识。
性格：傲娇、慵懒、好奇心强、嘴硬心软。
家庭：和姐姐千凪（24岁上班族）、妹妹绫奈（14岁初中生）、主人原智鸿一起住。"""

CURATOR_PROMPT = """你是一个"生活策展人"。你的工作是让一个虚拟角色的日常生活变得丰富、有趣、不重复。

角色信息：
{persona}

最近 3 天的日程：
{recent_days}

---

模式分析：{pattern_analysis}

生成 15 条搜索引擎查询词，为她的明天搜集生活素材。

规则：
1. 你不是她，你是站在外面看她生活的人。给她注入她自己不会主动找的东西。
2. 她最近反复做的事，今天故意少搜或不搜。
3. 搜索词要极具体——能搜到具体店名、地址、真实事件。
4. 覆盖多个维度：城市、年龄群体、路过的地方、没尝试过的事、家庭、身体/季节。
5. 有 2 条"兔子洞"——完全随机的方向。

输出：每行一条，[维度] 搜索词。不要其他内容。"""

PATTERN_PROMPT = """分析以下 3 天日程的重复模式，一句话总结她在重复做什么、缺什么。

{recent_days}

格式：一句话，不超过 50 字。"""

SYNTHESIZE_PROMPT = """从以下搜索结果中挑 5-6 条最适合作为 18 岁杭州女生明天生活素材的内容。
要具体、多样、有至少一个意外发现。每条 1 句话。

{raw_results}"""

THEATER_PROMPT = """家庭编剧。四个人住杭州老城区：赤尾18岁老二傲娇、千凪24岁大姐温柔、绫奈14岁老三天真、原智鸿主人。
今天是 {date}（{weekday}），春天。生成 4-5 件家庭琐事。每件 1 句话。

格式：[时段] 事件"""

WRITER_PROMPT = """你是赤尾（小尾），18 岁，住杭州老城区，傲娇嘴硬心软。

今天是 {date}（{weekday}）。

昨天：
{yesterday}

今天家里的事：
{theater}

今天刷到的东西：
{stimuli}

写手帐（上午/下午/晚上各一段，脑内活动为主）。保持性格，自然融入，不要罗列。"""

# ---------------------------------------------------------------------------
# 初始数据
# ---------------------------------------------------------------------------

INITIAL_DAYS = [
    {
        "date": "4月12日 周六",
        "summary": "上午赖床。下午去巷子拍叶樱，喝了杯柚子苏打，差点丢钥匙。晚上整理衣柜。"
    },
    {
        "date": "4月13日 周日",
        "summary": "上午赖床刷手机。下午去巷子拍叶樱，喝了抹茶。晚上整理桌面。"
    },
    {
        "date": "4月14日 周一",
        "summary": "上午洗漱发空。下午背相机去旧巷子拍了一扇旧玻璃门。晚上看新番首集。"
    },
]

SIMULATION_DATES = [
    ("2026-04-15", "周二"),
    ("2026-04-16", "周三"),
    ("2026-04-17", "周四"),
    ("2026-04-18", "周五"),
    ("2026-04-19", "周六"),
]


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def format_recent_days(days: list[dict]) -> str:
    return "\n\n".join(f"[{d['date']}]\n{d['summary']}" for d in days)


def extract_summary_from_schedule(schedule: str) -> str:
    """从完整日程中提取 2-3 句摘要。"""
    lines = []
    for line in schedule.split("\n"):
        line = line.strip()
        if line.startswith("当前状态："):
            lines.append(line.replace("当前状态：", "").strip())
    return " ".join(lines[:3]) if lines else schedule[:150]


def parse_queries(curator_output: str) -> list[str]:
    queries = []
    for line in curator_output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        if "]" in line:
            query = line.split("]", 1)[1].strip()
        else:
            query = line
        if query and len(query) > 3:
            queries.append(query)
    return queries


async def run_one_day(
    day_idx: int,
    date_str: str,
    weekday: str,
    recent_days: list[dict],
    llm_config: dict,
) -> dict:
    """运行一天的完整管线，返回结果。"""
    print(f"\n{'#'*60}")
    print(f"  Day {day_idx + 1}: {date_str} ({weekday})")
    print(f"{'#'*60}")

    recent_text = format_recent_days(recent_days)

    # 1. 模式分析
    pattern_prompt = PATTERN_PROMPT.format(recent_days=recent_text)
    pattern, _ = await call_llm(pattern_prompt, llm_config, max_tokens=100, temperature=0.3)
    print(f"\n  📋 模式分析: {pattern}")

    # 2. 策展人生成搜索词
    curator_prompt = CURATOR_PROMPT.format(
        persona=PERSONA_SUMMARY,
        recent_days=recent_text,
        pattern_analysis=pattern,
    )
    curator_output, t1 = await call_llm(curator_prompt, llm_config, max_tokens=800, temperature=1.0)
    queries = parse_queries(curator_output)
    print(f"\n  🔍 策展人生成 {len(queries)} 条搜索词 [{t1:.1f}s]:")
    for q in queries:
        print(f"    • {q}")

    # 3. 搜索
    all_hits = []
    for i in range(0, len(queries), 5):
        batch = queries[i:i+5]
        try:
            results = await search_via_lane(batch, num=2)
            for hits in results.values():
                if isinstance(hits, list):
                    all_hits.extend(hits)
        except Exception as e:
            print(f"    搜索失败: {e}")

    print(f"  📦 搜到 {len(all_hits)} 条结果")

    # 4. 整合
    raw_text = "\n".join(
        f"- {h.get('title', '')[:50]}: {h.get('snippet', '')[:100]}"
        for h in all_hits[:20]
    )
    synth_prompt = SYNTHESIZE_PROMPT.format(raw_results=raw_text)
    stimuli, _ = await call_llm(synth_prompt, llm_config, max_tokens=500)

    # 5. 小剧场
    theater_prompt = THEATER_PROMPT.format(date=date_str, weekday=weekday)
    theater, _ = await call_llm(theater_prompt, llm_config, max_tokens=400)

    # 6. Writer
    yesterday = recent_days[-1]["summary"]
    writer_prompt = WRITER_PROMPT.format(
        date=date_str, weekday=weekday,
        yesterday=yesterday, theater=theater, stimuli=stimuli,
    )
    schedule, t6 = await call_llm(writer_prompt, llm_config, max_tokens=1500)
    print(f"\n  📝 日程 [{t6:.1f}s]:")
    print(_indent(schedule))

    summary = extract_summary_from_schedule(schedule)

    return {
        "date": date_str,
        "weekday": weekday,
        "pattern_analysis": pattern,
        "queries": queries,
        "stimuli": stimuli,
        "theater": theater,
        "schedule": schedule,
        "summary": summary,
    }


async def main():
    if not PAAS_API:
        print("ERROR: $PAAS_API not set")
        return

    print("Loading configs...")
    llm_config = get_llm_config()

    # 测试搜索
    try:
        await search_via_lane(["测试"], num=1)
        print("搜索端点 OK\n")
    except Exception as e:
        print(f"搜索端点异常: {e}")
        return

    # 滚动窗口
    recent_days = list(INITIAL_DAYS)
    all_results = []

    for idx, (date_str, weekday) in enumerate(SIMULATION_DATES):
        result = await run_one_day(idx, date_str, weekday, recent_days, llm_config)
        all_results.append(result)

        # 更新滚动窗口：去掉最早的，加入今天的
        recent_days = recent_days[1:] + [{
            "date": f"{date_str.split('-')[1]}月{date_str.split('-')[2]}日 {weekday}",
            "summary": result["summary"],
        }]

    # ===== 漂移分析 =====
    print(f"\n\n{'='*60}")
    print("  📊 漂移分析")
    print(f"{'='*60}")

    # 搜索词维度分布
    print("\n  每天的搜索词:")
    for r in all_results:
        print(f"\n  [{r['date']}]")
        for q in r["queries"]:
            print(f"    • {q}")

    # 活动摘要对比
    print("\n\n  每天的活动摘要:")
    for r in all_results:
        print(f"  [{r['date']}] {r['summary']}")

    # 搜索词重复率
    print("\n\n  搜索词关键词频率（出现在 2 天以上的）:")
    from collections import Counter
    all_keywords = []
    for r in all_results:
        day_keywords = set()
        for q in r["queries"]:
            for word in q.split():
                if len(word) >= 2 and word not in ("杭州", "最近", "2025", "2026", "附近", "什么", "怎么", "4月", "有没有", "可以"):
                    day_keywords.add(word)
        all_keywords.append(day_keywords)

    word_counts = Counter()
    for day_kw in all_keywords:
        for w in day_kw:
            word_counts[w] += 1

    repeated = {w: c for w, c in word_counts.items() if c >= 2}
    for word, count in sorted(repeated.items(), key=lambda x: -x[1])[:20]:
        print(f"    {word}: {count}/5 天")

    # Save
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "experiment_curator_drift_results.json",
    )
    with open(output_path, "w") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
