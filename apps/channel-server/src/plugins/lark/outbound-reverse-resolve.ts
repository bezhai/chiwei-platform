// 飞书出站反查（飞书适配器职责）。chat-response-worker 收到的回复带全局
// internal_*_id（入站走过契约链翻译过的）。这里用 IdentityResolver.toChannel
// 把全局 ID 反查回飞书裸 id，再喂回飞书 native 出站路径。
//
// 「全局 ID → 渠道裸 id」的反查动作本身（IdentityResolver.toChannel）是平台无关
// 的、住在 core；但「断言这条 id 属于飞书、产出飞书裸 id 形态」是飞书专属的出站
// 适配逻辑，故收进 plugins/lark。本模块只负责飞书的反查 + 边界断言。
//
// channel === 'lark' 边界断言：T6 接 QQ 后，若某条非飞书的全局 ID 误流到飞书出站
// 路径，必须在喂给飞书发送器**之前**炸出来，绝不把 QQ 的回复发到飞书（或反之）。
// toChannel 反查不到 → 抛 IdentityNotFoundError（fail-loud，设计文档「禁止静默
// 丢弃」的出站对偶），绝不静默发到错地方。

import type { IdentityResolver } from '@core/channels/identity-resolver';

const LARK_CHANNEL = 'lark';

export interface ReverseResolveOutboundInput {
    resolver: IdentityResolver;
    messageGlobalId: string;
    chatGlobalId: string;
    rootGlobalId: string | undefined;
}

export interface OutboundChannelRefs {
    channelMessageId: string;
    channelChatId: string;
    channelRootId: string | undefined;
}

function assertLarkChannel(channel: string, which: string): void {
    if (channel !== LARK_CHANNEL) {
        throw new Error(
            `outbound boundary assertion failed: ${which} resolved to channel ` +
                `"${channel}" but lark native sender expects "lark"; refusing to ` +
                `feed a non-lark id to the lark sender (fail-loud, no wrong-send)`,
        );
    }
}

export async function reverseResolveOutbound(
    input: ReverseResolveOutboundInput,
): Promise<OutboundChannelRefs> {
    // toChannel 反查不到会抛 IdentityNotFoundError —— 故意不兜底。
    const msgRef = await input.resolver.toChannel('message', input.messageGlobalId);
    assertLarkChannel(msgRef.channel, 'message');

    const chatRef = await input.resolver.toChannel('chat', input.chatGlobalId);
    assertLarkChannel(chatRef.channel, 'chat');

    let channelRootId: string | undefined;
    if (input.rootGlobalId) {
        const rootRef = await input.resolver.toChannel('message', input.rootGlobalId);
        assertLarkChannel(rootRef.channel, 'root');
        channelRootId = rootRef.channelId;
    }

    return {
        channelMessageId: msgRef.channelId,
        channelChatId: chatRef.channelId,
        channelRootId,
    };
}
