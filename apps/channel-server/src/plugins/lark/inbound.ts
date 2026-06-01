// 飞书入站适配：飞书原始事件 → 通用 InboundMessage。这是飞书入站耦合的唯一
// 收口处——im.message.receive_v1 / union_id / chat_id / challenge /
// verification_token / image_key / file_key 这些飞书字眼只允许出现在本文件内，
// core 契约层只见通用 InboundMessage / ContentItem / ThreadRef。
//
// 实现 contracts.ts 的 InboundAdapter 契约（handleHandshake / verify / parse）。
// 出站不在这里——出站已收进 plugins/lark 的 OutboundCapabilities。

import type { LarkMention, LarkReceiveMessage } from 'types/lark';
import type {
    TextContent,
    ImageContent,
    StickerContent,
    PostContent,
    MediaContent,
    FileContent,
    AudioContent,
} from 'types/content-types';
import type {
    ContentItem,
    InboundAdapter,
    InboundMessage,
    ThreadRef,
} from '@core/channels/contracts';

export const LARK_CHANNEL = 'lark';

// 飞书回调握手：url_verification 事件原样回 challenge；其余（含真实消息事件）
// 不是握手，返回 null 让上层继续走 verify/parse。复刻飞书现状语义。
function handleHandshake(raw: unknown): unknown | null {
    const r = raw as { type?: string; challenge?: string };
    if (r && r.type === 'url_verification' && typeof r.challenge === 'string') {
        return { challenge: r.challenge };
    }
    return null;
}

// 飞书回调安全靠 verification_token + encrypt_key 校验（解密 + token 比对）。
// HTTP/WS 入口由 plugins/lark/webhook/ingress 使用飞书 SDK 承接，进到本适配
// 的事件已经过 SDK 校验与解密，故这里保持纯转换。
function verify(_raw: unknown): boolean {
    return true;
}

// 纯转换：飞书原生事件 → 通用 InboundMessage，零 I/O。内容映射、mention →
// addressing_hints、字段抽取全是同步的；common_* 投影不在这里做，由 lark
// common projector 在入站接线点处理。
function parse(raw: LarkReceiveMessage): InboundMessage | null {
    const event = raw;
    if (!event?.message || !event.message.message_id) return null;

    // 飞书原生类型逐一映射到通用 ContentItem，绝不因接契约就把图片/富文本/
    // sticker/media/file/audio/合并转发/未知类型当没收到。
    const content = parseLarkContent(event.message.message_type, event.message.content);
    if (content.length === 0) return null;

    // 飞书 p2p → direct，其余（group）→ group。
    const conversationScope = event.message.chat_type === 'p2p' ? 'direct' : 'group';

    // 飞书 mention → addressing_hints。targetId 用 union_id 口径，与 addressing
    // 的 hasMention(robot_union_id) 比对同源（见 addressing.ts）。
    const mentions: LarkMention[] = event.message.mentions ?? [];
    const addressingHints = mentions.map((m) => ({ targetId: m.id.union_id! }));

    // 飞书出站现状是 reply(message_id, content, replyInThread=true)——回复触发那
    // 条消息本身、留在话题串内。所以入站消息自身就是回复锚点：selfChannelMessageId
    // 永远填本条 message_id，inThread 永远 true。parent/root 也带上作为回退锚点。
    const threadRef: ThreadRef = {
        selfChannelMessageId: event.message.message_id,
        inThread: true,
    };
    if (event.message.parent_id) {
        threadRef.replyToChannelMessageId = event.message.parent_id;
    }
    if (event.message.root_id) {
        threadRef.rootChannelMessageId = event.message.root_id;
    }

    return {
        channel: LARK_CHANNEL,
        bot_name: event.app_id ?? '',
        channel_message_id: event.message.message_id,
        channel_chat_id: event.message.chat_id,
        channel_user_id: event.sender.sender_id?.open_id ?? 'unknown_sender',
        conversation_scope: conversationScope,
        thread_ref: threadRef,
        addressing_hints: addressingHints,
        content,
        received_at: Number(event.message.create_time) || 0,
    };
}

// 飞书原生消息类型 → 通用 ContentItem[]。飞书专有字段名（image_key/file_key/
// zh_cn 等）只在本函数内出现；merge_forward/share_chat/share_user/未知类型 →
// unsupported（保留 original_type，绝不静默丢弃）。
function parseLarkContent(messageType: string, rawContent: string): ContentItem[] {
    switch (messageType) {
        case 'text': {
            try {
                const c: TextContent = JSON.parse(rawContent);
                return [{ kind: 'text', text: c.text }];
            } catch (err) {
                console.error('Failed to parse text content:', err);
                return [{ kind: 'text', text: '[文本]' }];
            }
        }
        case 'image': {
            try {
                const c: ImageContent = JSON.parse(rawContent);
                return [{ kind: 'image', key: c.image_key }];
            } catch (err) {
                console.error('Failed to parse image content:', err);
                return [{ kind: 'text', text: '[图片]' }];
            }
        }
        case 'sticker': {
            try {
                const c: StickerContent = JSON.parse(rawContent);
                return [{ kind: 'sticker', key: c.file_key }];
            } catch (err) {
                console.error('Failed to parse sticker content:', err);
                return [{ kind: 'text', text: '[表情包]' }];
            }
        }
        case 'post': {
            try {
                const c: PostContent = JSON.parse(rawContent);
                const items: ContentItem[] = [];
                c.content.forEach((row) => {
                    row.forEach((node) => {
                        if (node.tag === 'text' && node.text) {
                            items.push({ kind: 'text', text: node.text });
                        } else if (node.tag === 'img' && node.image_key) {
                            items.push({ kind: 'image', key: node.image_key });
                        }
                    });
                });
                return items.length > 0 ? items : [{ kind: 'text', text: '[富文本]' }];
            } catch (err) {
                console.error('Failed to parse post content:', err);
                return [{ kind: 'text', text: '[富文本]' }];
            }
        }
        case 'media': {
            try {
                const c: MediaContent = JSON.parse(rawContent);
                return [
                    {
                        kind: 'file',
                        key: c.file_key,
                        meta: {
                            image_key: c.image_key,
                            file_name: c.file_name,
                            duration: c.duration,
                            lark_type: 'media',
                        },
                    },
                ];
            } catch (err) {
                console.error('Failed to parse media content:', err);
                return [{ kind: 'text', text: '[视频]' }];
            }
        }
        case 'file': {
            try {
                const c: FileContent = JSON.parse(rawContent);
                return [
                    {
                        kind: 'file',
                        key: c.file_key,
                        meta: { file_name: c.file_name, lark_type: 'file' },
                    },
                ];
            } catch (err) {
                console.error('Failed to parse file content:', err);
                return [{ kind: 'text', text: '[文件]' }];
            }
        }
        case 'audio': {
            try {
                const c: AudioContent = JSON.parse(rawContent);
                return [{ kind: 'audio', key: c.file_key, meta: { duration: c.duration } }];
            } catch (err) {
                console.error('Failed to parse audio content:', err);
                return [{ kind: 'text', text: '[语音]' }];
            }
        }
        case 'merge_forward':
            return [
                { kind: 'unsupported', text: '[合并转发]', meta: { original_type: 'merge_forward' } },
            ];
        case 'share_chat':
            return [
                { kind: 'unsupported', text: '[分享群名片]', meta: { original_type: 'share_chat' } },
            ];
        case 'share_user':
            return [
                { kind: 'unsupported', text: '[分享个人名片]', meta: { original_type: 'share_user' } },
            ];
        default:
            return [
                { kind: 'unsupported', text: `[${messageType}]`, meta: { original_type: messageType } },
            ];
    }
}

export const larkInbound: InboundAdapter = {
    handleHandshake,
    verify,
    parse,
};
