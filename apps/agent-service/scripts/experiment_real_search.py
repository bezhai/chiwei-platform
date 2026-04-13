"""实验：真实搜索策略对比 — 通过泳道上的 /admin/search 端点调真实 You Search API

用法: cd apps/agent-service && uv run python scripts/experiment_real_search.py

需要: agent-service 已部署到 exp 泳道
"""

import asyncio
import json
import os
import subprocess
import time

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        "SELECT api_key, base_url, client_type FROM model_provider WHERE name='azure'"
    )
    row = data["rows"][0]
    return {"api_key": row[0], "base_url": row[1]}


async def search_via_lane(queries: list[str], num: int = 5) -> dict:
    """Call /admin/search on the exp lane via PAAS_API."""
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
                "max_tokens": 1500,
            },
        )
        resp.raise_for_status()
        data = resp.json()
    elapsed = time.monotonic() - t0
    return data["choices"][0]["message"]["content"].strip(), elapsed


# ---------------------------------------------------------------------------
# 搜索策略
# ---------------------------------------------------------------------------

STRATEGIES = {
    "B: 本地生活": [
        "杭州 南宋御街 新开的店 2026年4月",
        "杭州老城区 小店 关门 拆迁 最近",
        "杭州 本地人 推荐 私藏 小红书",
    ],
    "C: 兴趣交叉": [
        "胶片摄影 手机摄影 讨论 2026",
        "2026年4月新番 豆瓣评分 推荐",
        "城市 有趣细节 井盖 路牌 设计",
    ],
    "D: 社交热帖": [
        "小红书 杭州 意外发现 2026",
        "微博 城市 让人停下来 最近",
        "豆瓣 最近在看 2026春",
    ],
    "E: 随机探索": [
        "杭州 二手 唱片 黑胶 vintage",
        "手帐 文具 杭州 实体店",
        "杭州 深夜 还开着的店 凌晨",
    ],
}

SYNTHESIZE_PROMPT = """你是一个素材编辑。以下是搜索收集到的原始结果。

请从中挑选最有趣、最具体、最能激发一个 18 岁杭州女生日常灵感的 5 条素材。

筛选标准：
- 具体到能变成"今天做了什么"的场景（不是抽象新闻）
- 优先本地的、小众的、有画面感的
- 排除太泛的东西（"春天到了""花粉高"）

原始搜索结果：
{raw_results}

输出：每条一行，写得像朋友发消息那样具体自然。"""


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


async def main():
    if not PAAS_API:
        print("ERROR: $PAAS_API not set")
        return

    print("Loading LLM config...")
    llm_config = get_llm_config()

    # 先测试搜索端点是否可用
    print(f"\n测试搜索端点 ({PAAS_API}/agent-service/admin/search, lane={LANE})...")
    try:
        test = await search_via_lane(["杭州天气"], num=2)
        print(f"  ✅ 搜索可用，返回 {len(test)} 个查询的结果")
    except Exception as e:
        print(f"  ❌ 搜索不可用: {e}")
        print("  确认 agent-service 已部署到 exp 泳道")
        return

    all_results = {}

    for strategy_name, queries in STRATEGIES.items():
        print(f"\n{'='*70}")
        print(f"  {strategy_name}")
        print(f"{'='*70}")

        # 批量搜索
        print(f"  搜索 {len(queries)} 个查询...")
        try:
            raw = await search_via_lane(queries, num=5)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        # 展示原始结果
        raw_texts = []
        for query, hits in raw.items():
            print(f"\n  🔍 {query}")
            if isinstance(hits, dict) and "error" in hits:
                print(f"    ERROR: {hits['error']}")
                continue
            for h in hits[:3]:
                title = h.get("title", "")[:60]
                snippet = h.get("snippet", "")[:100]
                print(f"    - {title}")
                if snippet:
                    print(f"      {snippet}")
                raw_texts.append(f"[{h.get('title', '')}] {h.get('snippet', '')}")

        # LLM 综合筛选
        if raw_texts:
            print(f"\n  📝 综合筛选...")
            combined = "\n\n".join(raw_texts)
            synth_prompt = SYNTHESIZE_PROMPT.format(raw_results=combined)
            try:
                synthesized, elapsed = await call_llm(synth_prompt, llm_config)
                print(f"  [{elapsed:.1f}s]")
                print(_indent(synthesized, "  ✅ "))
                all_results[strategy_name] = {
                    "raw_count": sum(
                        len(h) for h in raw.values() if isinstance(h, list)
                    ),
                    "synthesized": synthesized,
                }
            except Exception as e:
                print(f"  ERROR: {e}")

    # Save
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "experiment_real_search_results.json",
    )
    with open(output_path, "w") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
