"""persona 漂移 diff 飞书推送 — 落版之后告诉 bezhai「她变了哪一笔」(spec 决策 6).

persona review（:mod:`app.life.persona_review`）每落一版 source='review' 的身份
正文，把新旧版对照推到 bezhai 指定的飞书会话——他是回路外 reviewer：自动生效、
事后监督，不满意随时在版本链上盖 owner 版。

出站契约（Task 3 实证结论）
---------------------------
agent-service 唯一的飞书出口是 chat_response 队列
（``wire(ChatResponseSegment).to(Sink.mq("chat_response"))``，消费方
chat-response-worker）。这里 emit 一条**合成** :class:`ChatResponseSegment`：

  * ``message_id = persona-review:{lane}:{persona_id}:v{version}``——没有 inbound
    消息，合成 id 从 (lane, persona, version) 确定性派生：重推同版本撞同一个
    联合 Key (message_id, persona_id, part_index)，被 dedup 挡住。
  * ``is_proactive=True`` 且 ``root_id=None``——worker 据此走「跳过 message 维度
    反查、sendText 直发会话」分支（合成 id 在 lark_message 没有行，message 维度
    反查必炸；conversation 维度仍反查、解析失败 fail-loud）。
  * ``bot_name`` 显式携带——合成消息没有 agent_response 行，worker 的兜底
    （``payload.bot_name || agentResponse?.bot_name``）找不到 bot 就丢弃消息。
  * ``lane`` 显式带在 body——sink dispatch 不注入 header lane，worker 按
    body.lane 建上下文；mq publish 的泳道路由同样取它（contextvar 缺省时）。
  * ``session_id`` 设为合成 message_id（非空）——worker 拿 session_id 查
    common_agent_response，TypeORM 会把 undefined 条件静默丢掉、捞回任意行；
    非空保证查不到时干净返回 null，所有 agent_response 写路径天然跳过。

配置（Dynamic Config）
----------------------
  * key：``persona_review_notify``
  * 值：``{chat_id}|{bot_name}``——竖线分隔两段。``chat_id`` 是目标会话的
    **common_conversation_id**（UUID，不是飞书裸 oc_* id）；``bot_name`` 是发信
    bot 的注册名（multi-bot manager 认得的名字）。
    例：``018f0000-aaaa-bbbb-cccc-000000000001|chiwei_dev``。
  * 空 / 缺省 = 不推（info 留痕）——「只落库不通知」是缺省态。
  * 形状不对（没有竖线 / 半边为空）= 配置错误：不推 + error 留痕（要可感知，
    与故意留空区分开）。

fail-open 铁律：推送只是事后通知，**任何**一步（读配置 / 拼文本 / emit）失败都
绝不向上抛——版本已经落了，慢漂成功与否跟通知无关。
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import time

from inner_shared.dynamic_config import dynamic_config

from app.domain.chat_dataflow import ChatResponseSegment
from app.runtime.emit import emit  # module-level so tests can monkeypatch

logger = logging.getLogger(__name__)

PERSONA_REVIEW_NOTIFY_KEY = "persona_review_notify"


def persona_diff_message_id(lane: str, persona_id: str, version: int) -> str:
    """合成消息 id：从 (lane, persona, version) 确定性派生。

    它是 ChatResponseSegment 联合 Key 的 message_id 分量——重推同版本撞同
    Key、被 dedup 挡住，推送天然按版本幂等。
    """
    return f"persona-review:{lane}:{persona_id}:v{version}"


def parse_notify_target(raw: str) -> tuple[str, str] | None:
    """配置串 -> (chat_id, bot_name)。

    空串 / 全空白 = 未配置，返回 None（缺省不推是预期态）。形状不对（没有
    竖线 / 半边为空）抛 ValueError——由 push 的 fail-open 接住并 error 留痕，
    配置写错要可感知。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    chat_id, sep, bot_name = raw.partition("|")
    chat_id = chat_id.strip()
    bot_name = bot_name.strip()
    if not sep or not chat_id or not bot_name:
        raise ValueError(
            f"dynamic config {PERSONA_REVIEW_NOTIFY_KEY} 形状不对（应为 "
            f"'chat_id|bot_name'）：{raw!r}"
        )
    return chat_id, bot_name


def _unified_diff_block(
    old_narrative: str, new_narrative: str, *, prev: int, version: int
) -> str:
    """两版正文的 unified diff（上下文 2 行）；无差异返回空串（调用方明说无变化）。"""
    return "\n".join(
        difflib.unified_diff(
            old_narrative.splitlines(),
            new_narrative.splitlines(),
            fromfile=f"v{prev}",
            tofile=f"v{version}",
            n=2,
            lineterm="",
        )
    )


def render_persona_diff_text(
    *,
    lane: str,
    persona_id: str,
    old_narrative: str,
    new_narrative: str,
    version: int,
) -> str:
    """diff 消息文本：变化摘要（unified diff）在前 + 新版全文在后（纯文本）。

    owner 要一眼看到哪儿变了：变化摘要是 v{n-1} → v{n} 的 unified diff 块
    （上下文 2 行，- 旧行 / + 新行），原样重写（diff 为空）时明说「本周无变化、
    原样保留」。两版全文可能都不短：只带新版全文，旧版全文不发（长度控制，
    v{n-1} 在 PersonaVersion 链上随时可查）——消息总长 = diff 块 + 一版全文。
    """
    prev = version - 1
    diff_block = _unified_diff_block(
        old_narrative, new_narrative, prev=prev, version=version
    )
    if diff_block:
        change_section = f"——变化摘要（v{prev} → v{version}）——\n{diff_block}"
    else:
        change_section = f"本周无变化：与 v{prev} 完全一致，原样保留。"
    return (
        f"【persona 慢漂】{persona_id} 写下了新一版身份正文"
        f"（v{version}，lane={lane}）\n"
        f"变化：v{prev} → v{version}，全文 {len(old_narrative)} 字 → "
        f"{len(new_narrative)} 字。\n\n"
        f"{change_section}\n\n"
        f"——新一版全文——\n{new_narrative}"
    )


async def push_persona_diff(
    *,
    lane: str,
    persona_id: str,
    old_narrative: str,
    new_narrative: str,
    version: int,
) -> None:
    """把一次 persona 慢漂的新旧对照推到飞书。**本函数绝不向上抛**。

    Dynamic Config 的拉取是同步 httpx（10s 缓存），走 ``asyncio.to_thread``
    避免缓存刷新那一次阻塞事件循环（同 feed_whitelist 姿势）。缺省配置 =
    不推（info）；其余任何异常 = error 留痕后吞掉——版本已落，推送失败不影响
    慢漂本身。
    """
    try:
        raw = await asyncio.to_thread(
            dynamic_config.get, PERSONA_REVIEW_NOTIFY_KEY, default="",
        )
        target = parse_notify_target(raw)
        if target is None:
            logger.info(
                "[persona_diff_push] %s/%s dynamic config %s 未配置，"
                "本次落版只落库不推送",
                lane,
                persona_id,
                PERSONA_REVIEW_NOTIFY_KEY,
            )
            return
        chat_id, bot_name = target
        message_id = persona_diff_message_id(lane, persona_id, version)
        text = render_persona_diff_text(
            lane=lane,
            persona_id=persona_id,
            old_narrative=old_narrative,
            new_narrative=new_narrative,
            version=version,
        )
        await emit(
            ChatResponseSegment(
                message_id=message_id,
                persona_id=persona_id,
                part_index=0,
                session_id=message_id,
                chat_id=chat_id,
                is_p2p=False,
                root_id=None,
                user_id=None,
                is_proactive=True,
                bot_name=bot_name,
                lane=lane,
                content=text,
                status="success",
                is_last=True,
                full_content=text,
                published_at=int(time.time() * 1000),
            )
        )
        logger.info(
            "[persona_diff_push] %s/%s v%s diff 已投 chat_response"
            "（chat=%s, bot=%s）",
            lane,
            persona_id,
            version,
            chat_id,
            bot_name,
        )
    except Exception:
        logger.error(
            "[persona_diff_push] %s/%s v%s diff 推送失败"
            "（fail-open：版本已落，不影响慢漂本身）",
            lane,
            persona_id,
            version,
            exc_info=True,
        )
