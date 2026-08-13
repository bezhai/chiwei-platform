// 一条飞书消息在规则层的样子。
//
// 规则引擎是渠道无关的：它只认 common_* id、"这条冲不冲我来"、以及几个文本问题。
// 飞书的 om_id / oc_id / open_id 到这里为止 —— 往下再没有任何东西认识它们。
//
//     解析产出（reading）  ──┐
//                            ├──▶ RuleMessage ──▶ runRulesWith
//     投影产出（projection）─┘
//
// 两个来源分工很清楚：**身份全部来自投影**（它是唯一有权把飞书 id 换成公共层 id 的
// 地方），**正文全部来自解析**。当前 bot 是谁由调用方给 —— 同一条群消息会被同群的
// 几个 bot 各处理一遍，每个 bot 得到的是一份 botCommonUserId 不同的 RuleMessage。
//
// 文本访问器建在 `reading.content`（飞书原生片段）上，**不是** `reading.inbound.content`。
// 后者是给下游看的通用契约，@ 已经内联回了文本；拿它建 clearText 的话
// "@赤尾 余额" 读出来是含名字的一串，`EqualText('余额')` 这类指令全部失配。

import type { RuleMessage } from '@inner/shared/rules';

import { LARK_CHANNEL } from '../channel';
import {
    larkClearText,
    larkImageKeys,
    larkIsStickerOnly,
    larkIsTextOnly,
    larkStickerKey,
    larkText,
    larkWithoutEmojiText,
} from '../message/lark-content';
import type { LarkMessageReading } from '../message/read-message-event';
import type { LarkInboundProjection } from '../projection/inbound-projection';

/** 当前正在处理这条消息的 bot。 */
export interface LarkRuleMessageBot {
    botName: string;
    /** 它在 common_user 里的身份。群聊的"这条冲不冲我来"就是拿它跟被 @ 的人比。 */
    commonUserId: string;
}

export function larkRuleMessage(
    reading: LarkMessageReading,
    projection: LarkInboundProjection,
    bot: LarkRuleMessageBot,
): RuleMessage {
    const parts = reading.content;
    return {
        channel: LARK_CHANNEL,
        botName: bot.botName,

        commonUserId: projection.commonUserId,
        commonConversationId: projection.commonConversationId,
        commonMessageId: projection.commonMessageId,
        commonRootMessageId: projection.commonRootMessageId,

        // conversation_scope 是通用契约里"单聊还是群"的口径，由 chat_type 派生。
        isDirect: reading.inbound.conversation_scope === 'direct',
        botCommonUserId: bot.commonUserId,
        mentionedUserIds: projection.mentionedCommonUserIds,
        // 飞书的 create_time 是毫秒时间戳字符串。读不成数记 0，不让 NaN 流下去。
        createTime: Number(reading.message.createTime) || 0,

        clearText: () => larkClearText(parts),
        text: () => larkText(parts),
        withoutEmojiText: () => larkWithoutEmojiText(parts),
        isTextOnly: () => larkIsTextOnly(parts),
        isStickerOnly: () => larkIsStickerOnly(parts),
        stickerKey: () => larkStickerKey(parts),
        imageKeys: () => larkImageKeys(parts),
    };
}
