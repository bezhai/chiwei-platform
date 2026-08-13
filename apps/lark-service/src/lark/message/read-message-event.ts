// 解析层的收口：一条飞书消息事件进来，一次解析出去。
//
// 调用方只该看到这一个函数。谁先谁后、哪个投影从哪来，都在这里定死：
//
//     parseLarkMessage      事件 → 片段（纯函数，不查任何东西）
//     resolveLarkMentions   被 @ 的人叫什么（要查 bot 目录）
//     larkContentOf         投影一：飞书原生形态（@ 是独立片段）
//     inboundMessageOf      投影二：通用渠道契约（@ 内联回文本）
//
// 两个投影都是同一份片段的确定性函数，所以不存在"一边支持了新消息类型、另一边
// 忘了"这种状态 —— 新类型只加在 parseLarkMessage 的那一个 switch 里。

import type { InboundMessage } from '@inner/shared/channel';

import { larkContentOf, type LarkContentPart } from './lark-content';
import { inboundMessageOf } from './inbound-message';
import { resolveLarkMentions, type LarkBotLookup, type LarkMentionIndex } from './mentions';
import { parseLarkMessage, type LarkInboundMessage } from './parse-message';
import type { LarkMessageEvent } from './wire';

export interface LarkMessageReading {
    /** 飞书说了什么（事件里的事实，没有解释）。 */
    message: LarkInboundMessage;
    /** 被 @ 的人是谁、叫什么。 */
    mentions: LarkMentionIndex;
    /** 投影一：飞书原生正文。 */
    content: LarkContentPart[];
    /** 投影二：出了本服务之后大家说的话。 */
    inbound: InboundMessage;
}

export function readLarkMessageEvent(
    event: LarkMessageEvent,
    bots: LarkBotLookup,
): LarkMessageReading | null {
    const message = parseLarkMessage(event);
    if (!message) return null;

    const mentions = resolveLarkMentions(message.mentions, bots);
    return {
        message,
        mentions,
        content: larkContentOf(message, mentions),
        inbound: inboundMessageOf(message, mentions),
    };
}
