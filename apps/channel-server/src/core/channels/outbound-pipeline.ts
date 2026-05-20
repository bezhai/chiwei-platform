// 5b 出站接线（channel 无关核心）。chat-response-worker 收到的回复带全局
// internal_*_id（入站走过契约链翻译过的）。这里 IdentityResolver.toChannel
// 反查回 channel 裸 ID，再喂回各 channel 的 native 出站路径。
//
// 飞书出站沿用现状富文本 sendPost/replyPost（保留，不走 T2 纯文本
// OutboundAdapter）——避免富文本/图片回归（"飞书非文字/看图聊天零截断"
// 硬约束）。本函数只负责"全局 ID → 飞书裸 ID"反查 + channel 边界断言。
//
// channel === 'lark' 边界断言：T6 接 QQ 后，若某条非飞书的全局 ID 误流到
// 飞书出站路径，必须在喂给飞书发送器**之前**炸出来，绝不把 QQ 的回复发到
// 飞书（或反之）。toChannel 反查不到 → 抛 IdentityNotFoundError（fail-loud，
// 设计文档"禁止静默丢弃"的出站对偶），绝不静默发到错地方。

import type { IdentityResolver } from './identity-resolver';

export interface ReverseResolveForLarkInput {
    resolver: IdentityResolver;
    messageGlobalId: string;
    chatGlobalId: string;
    rootGlobalId: string | undefined;
}

export interface LarkReverseResolved {
    channelMessageId: string;
    channelChatId: string;
    channelRootId: string | undefined;
}

function assertLark(channel: string, which: string): void {
    if (channel !== 'lark') {
        throw new Error(
            `outbound boundary assertion failed: ${which} resolved to channel ` +
                `"${channel}" but lark native sender expects "lark"; refusing to ` +
                `feed a non-lark id to the lark sender (fail-loud, no wrong-send)`,
        );
    }
}

export async function reverseResolveForLark(
    input: ReverseResolveForLarkInput,
): Promise<LarkReverseResolved> {
    // toChannel 反查不到会抛 IdentityNotFoundError —— 故意不兜底。
    const msgRef = await input.resolver.toChannel('message', input.messageGlobalId);
    assertLark(msgRef.channel, 'message');

    const chatRef = await input.resolver.toChannel('chat', input.chatGlobalId);
    assertLark(chatRef.channel, 'chat');

    let channelRootId: string | undefined;
    if (input.rootGlobalId) {
        const rootRef = await input.resolver.toChannel('message', input.rootGlobalId);
        assertLark(rootRef.channel, 'root');
        channelRootId = rootRef.channelId;
    }

    return {
        channelMessageId: msgRef.channelId,
        channelChatId: chatRef.channelId,
        channelRootId,
    };
}
