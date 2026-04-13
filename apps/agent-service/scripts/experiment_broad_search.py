"""实验：大范围搜索探索 — 尝试各种不同方向的搜索策略

目标：找到什么样的搜索词能持续产出"有趣、具体、能变成生活场景"的素材

用法: cd apps/agent-service && uv run python scripts/experiment_broad_search.py
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


async def search_via_lane(queries: list[str], num: int = 5) -> dict:
    import httpx

    url = f"{PAAS_API}/api/agent/admin/search"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            url,
            json={"queries": queries, "num": num},
            headers={"x-lane": LANE},
        )
        resp.raise_for_status()
        return resp.json()


async def call_llm(prompt: str, config: dict) -> tuple[str, float]:
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
                "temperature": 0.7,
                "max_tokens": 1000,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    elapsed = time.monotonic() - t0
    return data["choices"][0]["message"]["content"].strip(), elapsed


# ---------------------------------------------------------------------------
# 搜索策略矩阵 — 覆盖尽可能多的维度
# ---------------------------------------------------------------------------

STRATEGIES = {
    # --- 真实信息源 ---
    "天气+环境": [
        "杭州今天天气 实时",
        "杭州 四月中旬 花 开了什么",
        "杭州 最近 日落时间 晚霞",
    ],
    "真实番剧": [
        "2026年4月新番 今天更新 第几集",
        "bilibili 本周新番 热度排行",
        "豆瓣 最近标记看过 动画 评分",
    ],
    "杭州本地事件": [
        "杭州 本周 展览 活动 2026年4月",
        "杭州 最近 新开 书店 咖啡 2026",
        "杭州 老街 整治 拆迁 搬迁 2026",
    ],

    # --- 生活场景 ---
    "日常采买/家务": [
        "杭州 菜场 哪个好逛 本地人",
        "一个人做饭 快手菜 不想动脑",
        "换季收纳 衣柜整理 懒人方法",
    ],
    "一个人能做的事": [
        "一个人 下午 去哪 不想逛商场",
        "适合自己待着的 安静地方 杭州",
        "无聊的下午 能做什么 有意思",
    ],
    "社交/朋友": [
        "周末约朋友 做什么 不贵 有趣",
        "姐妹聚会 在家能玩什么",
        "怎么安慰考试没考好的朋友",
    ],

    # --- 兴趣探索（跨出摄影） ---
    "音乐发现": [
        "最近好听的歌 2026 推荐 冷门",
        "city pop 入门 推荐 播放列表",
        "杭州 live house 最近演出",
    ],
    "阅读/知识": [
        "最近看的书 推荐 豆瓣 2026",
        "有意思的冷知识 让人停不下来",
        "日本文化 日常 有趣的细节",
    ],
    "手工/创作": [
        "手帐 灵感 2026 新玩法",
        "自己在家能做的手工 治愈",
        "贴纸 胶带 创意用法 日记",
    ],
    "美食探店": [
        "杭州 巷子里的店 推荐 不网红",
        "杭州 早餐 老店 本地人 排队",
        "春天 应季吃什么 时令美食",
    ],

    # --- 随机兔子洞（深夜好奇心） ---
    "奇怪的知识": [
        "为什么猫咪喜欢坐在纸上",
        "颜色名字的由来 为什么叫靛蓝",
        "城市里的野生动物 杭州 松鼠 刺猬",
    ],
    "互联网奇观": [
        "小红书 最近 很火 意想不到",
        "微博 今天 有趣的事 热搜以外",
        "b站 冷门 宝藏up主 推荐 2026",
    ],
}

RATE_PROMPT = """你是素材质量评审员。以下是一组搜索结果，请评估它们作为"18岁杭州女生日常生活素材"的质量。

搜索策略: {strategy_name}
搜索词: {queries}

搜索结果:
{results}

请评分并简述：
1. 具体度（1-5）：能不能直接变成"今天做了XX"的场景？
2. 新鲜度（1-5）：是新鲜的发现还是老生常谈？
3. 生活感（1-5）：像真人会遇到的事还是像新闻摘要？
4. 最佳素材：挑出最好的 1-2 条，说明为什么好
5. 改进建议：这类搜索应该怎么改搜索词才能更好？

简短回答，不超过 200 字。"""


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _format_hits(hits: list[dict]) -> str:
    lines = []
    for h in hits:
        title = h.get("title", "")[:80]
        snippet = h.get("snippet", "")[:150]
        lines.append(f"- {title}\n  {snippet}")
    return "\n".join(lines)


async def main():
    if not PAAS_API:
        print("ERROR: $PAAS_API not set")
        return

    print("Loading configs...")
    llm_config = get_llm_config()

    # 测试搜索
    print("测试搜索端点...")
    try:
        await search_via_lane(["测试"], num=1)
        print("  ✅ OK\n")
    except Exception as e:
        print(f"  ❌ {e}")
        return

    all_results = {}
    scores = {}

    for strategy_name, queries in STRATEGIES.items():
        print(f"\n{'─'*60}")
        print(f"  📂 {strategy_name}")
        print(f"{'─'*60}")

        # 搜索
        try:
            raw = await search_via_lane(queries, num=3)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        # 展示结果
        all_hits = []
        for query, hits in raw.items():
            if isinstance(hits, dict) and "error" in hits:
                print(f"  🔍 {query} → ERROR: {hits['error']}")
                continue
            print(f"  🔍 {query} → {len(hits)} 条")
            for h in hits[:2]:
                t = h.get("title", "")[:50]
                print(f"     • {t}")
            all_hits.extend(hits if isinstance(hits, list) else [])

        if not all_hits:
            continue

        # LLM 评分
        results_text = _format_hits(all_hits[:9])
        rate_prompt = RATE_PROMPT.format(
            strategy_name=strategy_name,
            queries=", ".join(queries),
            results=results_text,
        )
        try:
            rating, elapsed = await call_llm(rate_prompt, llm_config)
            print(f"\n  📊 评审 [{elapsed:.1f}s]:")
            print(_indent(rating, "  "))
            scores[strategy_name] = rating
        except Exception as e:
            print(f"  评审失败: {e}")

        all_results[strategy_name] = {
            "queries": queries,
            "hit_count": len(all_hits),
            "sample_titles": [h.get("title", "")[:60] for h in all_hits[:5]],
            "rating": scores.get(strategy_name, ""),
        }

    # 汇总排名
    print(f"\n\n{'='*60}")
    print("  📋 汇总")
    print(f"{'='*60}")
    for name, data in all_results.items():
        hits = data["hit_count"]
        print(f"\n  {name} ({hits} hits)")
        if data.get("rating"):
            # 提取评分行
            for line in data["rating"].split("\n"):
                if any(k in line for k in ["具体度", "新鲜度", "生活感", "最佳"]):
                    print(f"    {line.strip()}")

    # Save
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "experiment_broad_search_results.json",
    )
    with open(output_path, "w") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
