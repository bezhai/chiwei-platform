// `/chat_id` `/message_id` `/union_id`：把飞书那几个裸 id 念出来。
//
// 三条都是排查用的，没有副作用。它们**不判管理员** —— 与拆分前一致（同一组里只有
// `/block` 一族判）。这不是疏漏：会话 id 和消息 id 在群里本来就人人可见。
//
// 三条回复都**进话题**（上游那句 replyMessage 第三个参数是 true）。

import { firstMentionedHuman } from '../message/mentions';
import type { LarkCommandDeps, LarkSlashCommand } from '../rules/commands';

/** 这个会话的飞书 chat_id。 */
export function chatIdCommand(deps: LarkCommandDeps): LarkSlashCommand {
    return async (_message, context) => {
        await deps.api.replyText(context.message.messageId, context.message.chatId, true);
    };
}

/**
 * **被回复那条**消息的飞书 message_id，不是这条指令自己的。
 *
 * 念自己的 id 没有意义（发指令的人手上没有它），排查时要的一直是"我回复的那一条"。
 */
export function messageIdCommand(deps: LarkCommandDeps): LarkSlashCommand {
    return async (_message, context) => {
        const parentId = context.message.parentId;
        await deps.api.replyText(context.message.messageId, parentId || '消息不存在', true);
    };
}

/** 第一个被 @ 的真人的 union_id。 */
export function unionIdCommand(deps: LarkCommandDeps): LarkSlashCommand {
    return async (_message, context) => {
        const who = firstMentionedHuman(context.mentions);
        await deps.api.replyText(
            context.message.messageId,
            who ? `union_id: ${who}` : '请@具体用户进行获取union_id',
            true,
        );
    };
}
