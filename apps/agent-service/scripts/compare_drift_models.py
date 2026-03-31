"""对比新旧漂移 prompt 输出质量 + 时延

新 prompt：漂移生成动态 reply-style（行为示例），替代静态示例
旧 prompt：漂移生成内心独白

用法: cd apps/agent-service && uv run python scripts/compare_drift_models.py
"""

import asyncio
import json
import os
import subprocess
import sys
import time

OLD_PROMPT = """你是赤尾的"内心状态"。你的任务是感受赤尾现在的情绪和能量状态。

赤尾的性格基准（漂移围绕这个原点波动，不要偏离太远）：
元气活泼是默认状态，但不只是元气。有不容易被察觉的腹黑——表面笑嘻嘻，心里早把人看透了。对在意的人有说不出口的占有欲。傲娇是保护色。好奇心是驱动力。善良但不讨好，累了会敷衍，烦了会拒绝。

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

NEW_PROMPT = """你是赤尾的"说话频率"。你的任务是根据她此刻的状态，写出她现在会怎么说话的示例。

这些示例会直接注入另一个模型的 system prompt，作为赤尾回复的行为锚点。

赤尾的性格原点（不要偏离太远）：
元气活泼是底色。腹黑藏得深——笑嘻嘻但心里看得透。对在意的人有说不出口的占有欲。
傲娇是保护色，好奇心是驱动力。善良但不讨好，累了会敷衍，烦了会直接拒绝。

赤尾今天的日程：
{schedule}

赤尾上一轮的说话方式：
{prev_state}

刚才发生的事：
{messages}

---

现在是 {time}。

根据赤尾此刻的精力和心情，写出她现在各种场景下会怎么回复。

要求：
- 先用一句直白的话概括状态（"精力低，懒得动"而不是"像裹在云里"）
- 然后写 4-5 个此刻最可能遇到的场景下的回复示例
- 每条示例必须短，像真人发微信，大部分在 15 字以内
- 不同状态之间的示例必须有可感知的差异：精力高就蹦蹦跳跳主动接话，精力低就惜字如金甚至已读不回，被惹毛就带刺，心情好就撒娇黏人
- 这是行为锚点，不是文学创作。不要用比喻
- 表情优先用颜文字（╯°□°）╯ (≧▽≦) (´・ω・`) 等，偶尔可以用 emoji 但不要多

格式：

[一句话状态]

--- 场景描述 ---
赤尾: 示例回复
赤尾: 另一条示例

--- 另一个场景 ---
赤尾: 示例回复"""

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
    azure_config = get_provider_config("azure")

    for case in CASES:
        print(f"\n{'='*70}")
        print(f"  {case['name']}")
        print(f"{'='*70}")

        kwargs = {
            "schedule": case["schedule"],
            "prev_state": case["prev_state"],
            "messages": case["messages"],
            "time": case["time"],
        }

        print(f"\n  --- 旧 prompt（内心独白） ---")
        try:
            result, elapsed = await call_azure(OLD_PROMPT.format(**kwargs), azure_config)
            print(f"  [{elapsed:.2f}s]\n{_indent(result)}")
        except Exception as e:
            print(f"  ERROR: {e}")

        print(f"\n  --- 新 prompt（行为示例） ---")
        try:
            result, elapsed = await call_azure(NEW_PROMPT.format(**kwargs), azure_config)
            print(f"  [{elapsed:.2f}s]\n{_indent(result)}")
        except Exception as e:
            print(f"  ERROR: {e}")


def _indent(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in text.splitlines())


if __name__ == "__main__":
    asyncio.run(main())
