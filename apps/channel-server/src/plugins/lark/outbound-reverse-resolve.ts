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

// 会话独立反查：common_conversation_id → 飞书裸 chat_id，绝不碰 lark_message。
// 主动发（is_proactive）没有来源消息，只能拿真实会话 id 解析投递地址；用完整
// reverseResolveOutbound 会去查 lark_message（伪 proactive: id 必 miss、抛错）。
// 查不到 fail-loud，绝不静默把主动发的消息送到错地方。
export async function resolveLarkConversationRef(
    commonConversationId: string,
): Promise<{ channelId: string }> {
    const chat = await AppDataSource.getRepository(LarkBaseChatInfo).findOne({
        where: { common_conversation_id: commonConversationId },
    });
    if (!chat) {
        throw new Error(
            `lark outbound cannot resolve common_conversation_id=${commonConversationId}`,
        );
    }
    return { channelId: chat.chat_id };
}

export async function reverseResolveOutbound(
    input: ReverseResolveOutboundInput,
): Promise<OutboundChannelRefs> {
    const channelMessageId = await resolveLarkMessageRef(input.commonMessageId);

    const chat = await AppDataSource.getRepository(LarkBaseChatInfo).findOne({
        where: { common_conversation_id: input.commonConversationId },
    });
    if (!chat) {
        throw new Error(
            `lark outbound cannot resolve common_conversation_id=${input.commonConversationId}`,
        );
    }

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
        channelChatId: chat.chat_id,
        channelRootId,
    };
}
