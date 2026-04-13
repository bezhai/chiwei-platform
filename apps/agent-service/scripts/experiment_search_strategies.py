"""实验：搜索策略对比 — 什么样的搜索能抓到有趣的素材

测试不同搜索策略的素材质量：
  A: 泛搜索（当前 Ideation 的风格）
  B: 本地生活搜索（地名 + 生活场景）
  C: 兴趣交叉搜索（把人设兴趣和随机话题碰撞）
  D: 社交媒体热帖搜索（小红书/微博风格）

用法: cd apps/agent-service && uv run python scripts/experiment_search_strategies.py
"""

import asyncio
import json
import os
import subprocess
import time

# ---------------------------------------------------------------------------
# DB + LLM helpers
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


def get_search_config() -> dict:
    """Get You Search API config -- not available locally, return empty."""
    return {}


async def search_you(query: str, api_key: str, host: str, num: int = 5) -> list[dict]:
    """Direct You Search API call."""
    import httpx

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{host}/v1/search",
            params={"q": query, "gl": "CN", "hl": "ZH-HANS", "num": num},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()

    results = []
    for hit in data.get("results", data.get("hits", []))[:num]:
        results.append({
            "title": hit.get("title", ""),
            "url": hit.get("url", ""),
            "snippet": hit.get("snippet", hit.get("description", ""))[:200],
        })
    return results


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
    "A: 泛搜索（当前风格）": [
        "2026年4月 社交媒体热门话题",
        "杭州 四月 天气 花粉",
        "最近有什么有意思的事",
    ],
    "B: 本地生活搜索": [
        "杭州 南宋御街 新开的店 2026",
        "杭州老城区 小店 关门 拆迁 2026",
        "杭州 周末 去哪玩 本地人推荐 小红书",
    ],
    "C: 兴趣交叉搜索": [
        "胶片摄影 vs 手机摄影 讨论 2026",
        "2026年4月新番 推荐 豆瓣评分",
        "城市里有趣的细节 井盖 路牌 设计",
    ],
    "D: 社交媒体热帖": [
        "小红书 杭州 意外发现 最近",
        "微博 今天看到 让人停下来 城市",
        "豆瓣 最近在看 2026春季",
    ],
}

# ---------------------------------------------------------------------------
# 素材整合 prompt
# ---------------------------------------------------------------------------

SYNTHESIZE_PROMPT = """你是一个素材编辑。以下是通过搜索收集到的原始结果。

请从中挑选出最有趣、最具体、最能激发一个 18 岁杭州女生日常灵感的 5 条素材。

筛选标准：
- 要具体到能变成"今天做了什么"的场景（不是抽象的趋势报道）
- 优先本地的、小众的、有画面感的
- 排除太泛的新闻（"春天到了""花粉高"这种）

原始搜索结果：
{raw_results}

输出格式：每条一行，写得像朋友发来的消息那样具体自然。"""


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


async def main():
    print("Loading configs...")
    llm_config = get_provider_config("azure")

    # 尝试获取搜索 API 配置
    search_cfg = get_search_config()
    has_search = bool(search_cfg.get("you_search_host"))

    if not has_search:
        print("\n⚠️  You Search API 未配置，改用 LLM 模拟搜索结果")
        print("  (实际部署时会用真实搜索)")

    all_results = {}

    for strategy_name, queries in STRATEGIES.items():
        print(f"\n{'='*70}")
        print(f"  {strategy_name}")
        print(f"{'='*70}")

        raw_results = []

        for query in queries:
            print(f"\n  🔍 搜索: {query}")

            if has_search:
                try:
                    results = await search_you(
                        query,
                        search_cfg["you_search_api_key"],
                        search_cfg["you_search_host"],
                    )
                    for r in results:
                        print(f"    - {r['title'][:60]}")
                        raw_results.append(f"[{r['title']}] {r['snippet']}")
                except Exception as e:
                    print(f"    ERROR: {e}")
            else:
                # 用 LLM 模拟搜索结果
                sim_prompt = f"""模拟搜索引擎。对于查询"{query}"，生成 3 条真实感的搜索结果摘要。
每条包含标题和 1-2 句摘要。要具体、真实，像真的搜索结果。
不要泛泛而谈，给具体的店名、地名、人名、数据。"""
                try:
                    sim_result, _ = await call_llm(sim_prompt, llm_config)
                    print(_indent(sim_result, "    "))
                    raw_results.append(sim_result)
                except Exception as e:
                    print(f"    ERROR: {e}")

        # 用 LLM 综合筛选
        if raw_results:
            print(f"\n  📝 综合筛选...")
            combined = "\n\n".join(raw_results)
            synth_prompt = SYNTHESIZE_PROMPT.format(raw_results=combined)
            try:
                synthesized, elapsed = await call_llm(synth_prompt, llm_config)
                print(f"  [{elapsed:.1f}s]")
                print(_indent(synthesized, "  ✅ "))
                all_results[strategy_name] = synthesized
            except Exception as e:
                print(f"  ERROR: {e}")
                all_results[strategy_name] = f"ERROR: {e}"

    # Save
    output_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "experiment_search_results.json",
    )
    with open(output_path, "w") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"\n\nResults saved to {output_path}")


if __name__ == "__main__":
    asyncio.run(main())
