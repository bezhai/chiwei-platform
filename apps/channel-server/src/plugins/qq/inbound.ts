// QQ 入站 adapter（InboundAdapter 契约实现）。
//
// 本插件的入站来源不是 QQ 原始事件，而是 qq-gateway 已经归一化好的
// CustomInboundMessage（@inner/shared/protocols）——验签 / 握手 / accessToken /
// 媒体解码全在网关侧做完。所以这里：
//   - handleHandshake 恒 null（无握手需要本进程参与）。
//   - verify 恒 true（签名校验在网关做完，custom 协议进来已经是可信内网流量；
//     HTTP ingress 再经内网 Bearer 鉴权兜底，见 runtime.ts）。
//   - parse 把 CustomInboundMessage 翻成平台无关 InboundMessage。
//
// 群聊 @bot 唤起判定靠网关给的 mention.isSelf=true。AddressingPolicy 是按
// hint.targetId === botMentionTarget 比较，所以把 isSelf 折成一个哨兵 targetId
// （QQ_SELF_MENTION_TARGET），handler 用同一个哨兵作 botMentionTarget。这是 QQ
// 自己的「ID 口径」——其它 channel（飞书用 union_id）各自决定，契约层不感知。

import type { CustomInboundMessage, CustomInboundAttachment } from '@inner/shared/protocols';
import type {
    AddressingHint,
    ContentItem,
    InboundAdapter,
    InboundMessage,
    ThreadRef,
} from '@core/channels/contracts';

export const QQ_CHANNEL = 'qq';

// 群聊 @ 本 bot 的寻址哨兵。网关把「这条 @ 了我」表达成 mention.isSelf=true；
// 入站把它折成这个 targetId，addressing/handler 用同一个常量比较。
export const QQ_SELF_MENTION_TARGET = '__qq_self__';

function handleHandshake(_raw: unknown): unknown | null {
    return null;
}

function verify(_raw: unknown): boolean {
    return true;
}

function attachmentToContentItem(att: CustomInboundAttachment): ContentItem {
    const type = att.contentType ?? '';
    if (type.startsWith('image/')) {
        return { kind: 'image', key: att.url };
    }
    if (type.startsWith('audio/')) {
        return {
            kind: 'audio',
            key: att.url,
            meta: {
                content_type: type,
                voice_wav_url: att.voiceWavUrl,
                asr_text: att.asrText,
            },
        };
    }
    return {
        kind: 'file',
        key: att.url,
        meta: { content_type: type, file_name: att.filename },
    };
}

function buildContent(raw: CustomInboundMessage): ContentItem[] {
    const items: ContentItem[] = [];
    if (typeof raw.text === 'string' && raw.text.trim().length > 0) {
        items.push({ kind: 'text', text: raw.text });
    }
    for (const att of raw.attachments ?? []) {
        items.push(attachmentToContentItem(att));
    }
    return items;
}

function parse(raw: CustomInboundMessage): InboundMessage | null {
    if (!raw || !raw.messageId) return null;

    const content = buildContent(raw);
    if (content.length === 0) return null;

    const conversationScope = raw.chatType === 'direct' ? 'direct' : 'group';

    const addressingHints: AddressingHint[] = (raw.mentions ?? [])
        .map((m): AddressingHint => ({
            targetId: m.isSelf
                ? QQ_SELF_MENTION_TARGET
                : (m.memberId ?? m.userId ?? m.id ?? m.name ?? ''),
        }))
        .filter((h) => h.targetId.length > 0);

    // QQ 出站是被动回复触发那条消息：thread_ref 只在用户「引用回复」某条消息时
    // 才有锚点（quote）。无引用时传 null（契约要求 ThreadRef 非空必带锚点）。
    let threadRef: ThreadRef | null = null;
    const quotedId = raw.quote?.messageId ?? raw.quote?.refId;
    if (quotedId) {
        threadRef = { replyToChannelMessageId: quotedId };
    }

    const receivedAt = Date.parse(raw.timestamp);

    return {
        channel: QQ_CHANNEL,
        bot_name: raw.botName,
        channel_message_id: raw.messageId,
        channel_chat_id: raw.conversationId,
        channel_user_id: raw.senderId,
        conversation_scope: conversationScope,
        thread_ref: threadRef,
        addressing_hints: addressingHints,
        content,
        received_at: Number.isNaN(receivedAt) ? 0 : receivedAt,
    };
}

export const qqInbound: InboundAdapter = {
    handleHandshake,
    verify,
    parse,
};
