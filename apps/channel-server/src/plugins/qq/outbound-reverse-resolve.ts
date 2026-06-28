// QQ 出站反查（QQ 插件私有职责）。common 层只保存 common_*_id；要发回网关时，
// 只有 qq 插件能读 qq_* 私有表，把 common id 解析回 QQ 裸 id。查不到 fail-loud，
// 绝不静默把回复送到错地方（与飞书 outbound-reverse-resolve 同取向）。

import AppDataSource from 'ormconfig';
import { QqGroupChatInfo } from '@entities/qq-group-chat-info';
import { QqMessage } from '@entities/qq-message';

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

export async function resolveQqMessageRef(commonMessageId: string): Promise<string> {
    const msg = await AppDataSource.getRepository(QqMessage).findOne({
        where: { common_message_id: commonMessageId },
    });
    if (!msg) {
        throw new Error(`qq outbound cannot resolve common_message_id=${commonMessageId}`);
    }
    return msg.qq_message_id;
}

export async function resolveQqConversationRef(
    commonConversationId: string,
): Promise<{ channelId: string }> {
    const conv = await AppDataSource.getRepository(QqGroupChatInfo).findOne({
        where: { common_conversation_id: commonConversationId },
    });
    if (!conv) {
        throw new Error(
            `qq outbound cannot resolve common_conversation_id=${commonConversationId}`,
        );
    }
    return { channelId: conv.conversation_id };
}

export async function reverseResolveOutbound(
    input: ReverseResolveOutboundInput,
): Promise<OutboundChannelRefs> {
    const channelMessageId = await resolveQqMessageRef(input.commonMessageId);
    const conversation = await resolveQqConversationRef(input.commonConversationId);

    let channelRootId: string | undefined;
    if (input.commonRootMessageId) {
        try {
            channelRootId = await resolveQqMessageRef(input.commonRootMessageId);
        } catch (_e) {
            // QQ root 链可能未落映射；丢链不阻断（与飞书 root resolve 不同：QQ 出站
            // 只认 replyToMessageId，root 仅作观测，缺了不致命）。
            channelRootId = undefined;
        }
    }

    return {
        channelMessageId,
        channelChatId: conversation.channelId,
        channelRootId,
    };
}
