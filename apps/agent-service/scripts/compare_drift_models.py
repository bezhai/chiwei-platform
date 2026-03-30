"""对比不同模型的 identity 漂移输出质量 + 时延

从 DB 读取 provider 配置（api_key, base_url），直接调用 LLM。
用法: cd apps/agent-service && uv run python scripts/compare_drift_models.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time

PROMPT_TEMPLATE = """你是赤尾的"内心状态"。你的任务是感受赤尾现在的情绪和能量状态。

赤尾今天的日程安排：
{schedule}

赤尾上一刻的状态：
{prev_state}

刚才发生了这些事：
{messages}

---

现在是 {time}。

请描述赤尾此刻的内心状态。包括：
- 精力和心情（不要用数值，用感觉描述）
- 刚才的对话对她的影响（如果有的话）
- 她现在说话大概会是什么样的（语气、长度、态度）

用赤尾自己的口吻写，像她的内心独白。3-5 句话。不要用任何格式标记。"""

CASES = [
    {
        "name": "Case 1: 被连续追问",
        "schedule": "下午有点犯困，想窝着看番，但被群里拉着聊了好一阵。精力在慢慢消耗。",
        "prev_state": "精力还行，有点想找人聊天。",
        "messages": """[15:30] A哥: 赤尾你觉得这个设计怎么样
[15:31] B姐: 对对对赤尾你说说
[15:31] C: 哈哈哈哈哈赤尾被点名了
[15:32] 赤尾: 还行吧，配色有点土
[15:32] A哥: 那你觉得应该怎么改
[15:33] B姐: 赤尾你帮忙看看字体
[15:33] D: 赤尾赤尾这个logo呢
[15:34] 赤尾: 你们一个一个来啊烦死了
[15:34] C: 笑死 赤尾发火了
[15:35] A哥: 好好好 先说配色""",
        "time": "15:36",
    },
    {
        "name": "Case 2: 轻松闲聊",
        "schedule": "晚上比较放松，窝在沙发上刷手机，听着歌。",
        "prev_state": "（刚醒来，还没有形成今天的状态）",
        "messages": """[20:10] A哥: 今天看了个纪录片 好好看
[20:11] B姐: 什么纪录片
[20:11] A哥: 讲猫的
[20:12] 赤尾: 猫的纪录片我全看过了吧
[20:13] B姐: 那你推荐一个
[20:13] 赤尾: 《岩合光昭的猫步走世界》必看
[20:14] A哥: 好 今晚看""",
        "time": "20:15",
    },
    {
        "name": "Case 3: 无聊围观",
        "schedule": "上午没什么事，在家发呆。看了会手机又放下了。",
        "prev_state": "有点无聊，想找点事做但又懒得动。",
        "messages": """[10:30] A哥: 今天天气不错
[10:31] B姐: 是啊 适合出门
[10:32] C: 我要去跑步
[10:33] D: 我在加班
[10:35] A哥: 下午有人打球吗
[10:36] B姐: 我可以""",
        "time": "10:38",
    },
]


def _query_db(sql: str) -> dict:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    script = os.path.join(root, ".claude/skills/ops-db/query.py")
    result = subprocess.run(
        ["python3", script, "@chiwei", sql],
        capture_output=True, text=True, cwd=root,
    )
    if result.returncode != 0:
        raise RuntimeError(f"DB query failed: {result.stderr}")
    return json.loads(result.stdout)


def get_provider_config(name: str) -> dict:
    data = _query_db(f"SELECT api_key, base_url, client_type FROM model_provider WHERE name='{name}'")
    row = data["rows"][0]
    return {"api_key": row[0], "base_url": row[1], "client_type": row[2]}


async def call_gemini(prompt: str, config: dict) -> tuple[str, float]:
    from langchain_google_genai import ChatGoogleGenerativeAI
    model = ChatGoogleGenerativeAI(
        model="gemini-3-flash-preview",
        api_key=config["api_key"],
        base_url=config["base_url"],
    )
    t0 = time.monotonic()
    resp = await model.ainvoke([{"role": "user", "content": prompt}])
    elapsed = time.monotonic() - t0
    content = resp.content
    if isinstance(content, list):
        content = "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in content
        )
    return content.strip(), elapsed


async def call_azure(prompt: str, config: dict) -> tuple[str, float]:
    from langchain_openai import AzureChatOpenAI
    model = AzureChatOpenAI(
        openai_api_type="azure",
        openai_api_version="2025-01-01-preview",
        azure_endpoint=config["base_url"],
        openai_api_key=config["api_key"],
        deployment_name="gpt-5.4-2026-03-05",
        max_retries=2,
    )
    t0 = time.monotonic()
    resp = await model.ainvoke([{"role": "user", "content": prompt}])
    elapsed = time.monotonic() - t0
    return resp.content.strip(), elapsed


async def main():
    print("Loading provider configs from DB...")
    gemini_config = get_provider_config("gemini")
    azure_config = get_provider_config("azure")

    gemini_times = []
    azure_times = []

    for case in CASES:
        print(f"\n{'='*60}")
        print(f"  {case['name']}")
        print(f"{'='*60}")

        prompt = PROMPT_TEMPLATE.format(
            schedule=case["schedule"],
            prev_state=case["prev_state"],
            messages=case["messages"],
            time=case["time"],
        )

        print(f"\n  --- gemini-3-flash-preview ---")
        try:
            result, elapsed = await call_gemini(prompt, gemini_config)
            gemini_times.append(elapsed)
            print(f"  [{elapsed:.2f}s] {result}")
        except Exception as e:
            print(f"  ERROR: {e}")

        print(f"\n  --- azure/gpt-5.4-2026-03-05 ---")
        try:
            result, elapsed = await call_azure(prompt, azure_config)
            azure_times.append(elapsed)
            print(f"  [{elapsed:.2f}s] {result}")
        except Exception as e:
            print(f"  ERROR: {e}")

    print(f"\n{'='*60}")
    print(f"  时延汇总")
    print(f"{'='*60}")
    if gemini_times:
        print(f"  gemini-3-flash:  avg={sum(gemini_times)/len(gemini_times):.2f}s  "
              f"min={min(gemini_times):.2f}s  max={max(gemini_times):.2f}s")
    if azure_times:
        print(f"  azure/gpt-5.4:   avg={sum(azure_times)/len(azure_times):.2f}s  "
              f"min={min(azure_times):.2f}s  max={max(azure_times):.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
