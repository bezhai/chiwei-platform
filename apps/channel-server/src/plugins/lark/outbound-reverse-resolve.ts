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

export async function reverseResolveOutbound(
    input: ReverseResolveOutboundInput,
): Promise<OutboundChannelRefs> {
    const msg = await AppDataSource.getRepository(LarkMessage).findOne({
        where: { common_message_id: input.commonMessageId },
    });
    if (!msg) {
        throw new Error(
            `lark outbound cannot resolve common_message_id=${input.commonMessageId}`,
        );
    }

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
        const root = await AppDataSource.getRepository(LarkMessage).findOne({
            where: { common_message_id: input.commonRootMessageId },
        });
        if (!root) {
            throw new Error(
                `lark outbound cannot resolve root common_message_id=${input.commonRootMessageId}`,
            );
        }
        channelRootId = root.om_id;
    }

    return {
        channelMessageId: msg.om_id,
        channelChatId: chat.chat_id,
        channelRootId,
    };
}
