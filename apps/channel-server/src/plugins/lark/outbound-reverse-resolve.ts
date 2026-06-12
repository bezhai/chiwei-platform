// 飞书出站反查（飞书插件私有职责）。common 层只保存 common_*_id；需要调用
// 飞书 API 时，只有 lark 插件能读取 lark_* 表，把 common id 解析回飞书裸 id。
// agent-service / dashboard / core 都不能通过公共 identity 表反查 lark。

import AppDataSource from 'ormconfig';
import { LarkBaseChatInfo } from '@entities/lark-base-chat-info';
import { LarkMessage } from '@entities/lark-message';

export interface ReverseResolveOutboundInput {
    commonMessageId: string;
    commonConversationId: string;
    commonRootMessageId: string | undefined;
}

export interface OutboundChannelRefs {
    channelMessageId: string;
    channelChatId: string;
    channelRootId: string | undefined;
}

export async function resolveLarkMessageRef(commonMessageId: string): Promise<string> {
    const msg = await AppDataSource.getRepository(LarkMessage).findOne({
        where: { common_message_id: commonMessageId },
    });
    if (!msg) {
        throw new Error(
            `lark outbound cannot resolve common_message_id=${commonMessageId}`,
        );
    }
    return msg.om_id;
}

// 仅会话维度的反查：proactive 合成消息（message_id 是上游自造的全局 id、
// lark_message 没有行）只需要把 common_conversation_id 翻成飞书裸 chat_id。
// 反查不到照样 fail-loud——绝不静默把消息发进未知会话。
export async function resolveLarkChatId(commonConversationId: string): Promise<string> {
    const chat = await AppDataSource.getRepository(LarkBaseChatInfo).findOne({
        where: { common_conversation_id: commonConversationId },
    });
    if (!chat) {
        throw new Error(
            `lark outbound cannot resolve common_conversation_id=${commonConversationId}`,
        );
    }
    return chat.chat_id;
}

export async function reverseResolveOutbound(
    input: ReverseResolveOutboundInput,
): Promise<OutboundChannelRefs> {
    const channelMessageId = await resolveLarkMessageRef(input.commonMessageId);
    const channelChatId = await resolveLarkChatId(input.commonConversationId);

    let channelRootId: string | undefined;
    if (input.commonRootMessageId) {
        try {
            channelRootId = await resolveLarkMessageRef(input.commonRootMessageId);
        } catch (_e) {
            throw new Error(
                `lark outbound cannot resolve root common_message_id=${input.commonRootMessageId}`,
            );
        }
    }

    return {
        channelMessageId,
        channelChatId,
        channelRootId,
    };
}
