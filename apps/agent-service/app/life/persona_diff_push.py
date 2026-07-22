"""persona 漂移 diff 飞书推送 — 落版之后告诉 bezhai「她变了哪一笔」(spec 决策 6).

persona review（:mod:`app.life.persona_review`）每落一版 source='review' 的身份
正文，把新旧版对照推给 bezhai——他是回路外 reviewer：自动生效、事后监督，
不满意随时在版本链上盖 owner 版。

出站契约（bezhai 2026-06-13 拍板，取代先前 chat_response 合成 segment 方案）
------------------------------------------------------------------------------
直接 POST 飞书**自定义机器人 webhook**（告警专用 bot），不进对话链路：

  * URL 从环境变量 ``PERSONA_REVIEW_WEBHOOK_URL`` 读（PaaS App envs 注入；值
    是完整的飞书 incoming webhook 地址，形如
    ``https://open.feishu.cn/open-apis/bot/v2/hook/<token>``，token 属敏感配置
    不进 Dynamic Config 不进 git）。命名与 alert-webhook 服务的
    ``FEISHU_WEBHOOK_URL`` 呼应（同样的「完整 webhook URL 一个变量」约定），
    加 ``PERSONA_REVIEW_`` 前缀按用途限定。
  * env 缺省 / 空白 = 不推（info 留痕）——「只落库不通知」是缺省态。
  * 协议：``{"msg_type": "text", "content": {"text": "<消息文本>"}}``，10s 超时
    （同 alert-webhook 姿势）。
  * 失败可感知：HTTP 非 2xx、或飞书 body 错误码（``code != 0``——webhook 的
    失败常以 200 + 错误码形式出现）都 error 留痕。

fail-open 铁律：推送只是事后通知，**任何**一步（读 env / 拼文本 / POST）失败都
绝不向上抛——版本已经落了，慢漂成功与否跟通知无关。
"""

from __future__ import annotations

import difflib
import logging
import os

from app.capabilities.persona_notification import (
    PersonaNotificationOutcome,
    send_persona_notification,
)

logger = logging.getLogger(__name__)

PERSONA_REVIEW_WEBHOOK_URL_ENV = "PERSONA_REVIEW_WEBHOOK_URL"


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
    """把一次 persona 慢漂的新旧对照 POST 到飞书告警 webhook。**本函数绝不向上抛**。

    env ``PERSONA_REVIEW_WEBHOOK_URL`` 缺省 = 不推（info）；POST 失败（HTTP 非
    2xx / 飞书 body code != 0 / 连接异常）= error 留痕后吞掉——版本已落，推送
    失败不影响慢漂本身。
    """
    try:
        url = (os.getenv(PERSONA_REVIEW_WEBHOOK_URL_ENV) or "").strip()
        if not url:
            logger.info(
                "[persona_diff_push] %s/%s env %s 未配置，本次落版只落库不推送",
                lane,
                persona_id,
                PERSONA_REVIEW_WEBHOOK_URL_ENV,
            )
            return
        text = render_persona_diff_text(
            lane=lane,
            persona_id=persona_id,
            old_narrative=old_narrative,
            new_narrative=new_narrative,
            version=version,
        )
        result = await send_persona_notification(url=url, text=text)
        if result.outcome is PersonaNotificationOutcome.HTTP_ERROR:
            logger.error(
                "[persona_diff_push] %s/%s v%s webhook 返回 HTTP %d"
                "（fail-open：版本已落，不影响慢漂本身）：%s",
                lane,
                persona_id,
                version,
                result.status_code,
                result.response_preview,
            )
            return
        if result.outcome is PersonaNotificationOutcome.PROVIDER_ERROR:
            logger.error(
                "[persona_diff_push] %s/%s v%s 飞书返回错误码 %s"
                "（fail-open：版本已落，不影响慢漂本身）：%s",
                lane,
                persona_id,
                version,
                result.provider_code,
                result.provider_message,
            )
            return
        logger.info(
            "[persona_diff_push] %s/%s v%s diff 已推飞书告警 webhook",
            lane,
            persona_id,
            version,
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
