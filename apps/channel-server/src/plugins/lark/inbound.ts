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

function handleHandshake(raw: unknown): unknown | null {
    const r = raw as { type?: string; challenge?: string };
    if (r && r.type === 'url_verification' && typeof r.challenge === 'string') {
        return { challenge: r.challenge };
    }
    return null;
}

function verify(_raw: unknown): boolean {
    return true;
}

function parse(raw: LarkReceiveMessage): InboundMessage | null {
    const event = raw;
    if (!event?.message || !event.message.message_id) return null;

    const mentions: LarkMention[] = event.message.mentions ?? [];
    const content = parseLarkContent(event.message.message_type, event.message.content, mentions);
    if (content.length === 0) return null;

    const conversationScope = event.message.chat_type === 'p2p' ? 'direct' : 'group';

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

function mentionDisplay(m: LarkMention): string {
    return m.name?.trim() || m.id.union_id || m.id.user_id || m.id.open_id || m.key;
}

function applyMentionText(text: string, mentions: LarkMention[]): string {
    const byKey = new Map(mentions.map((m) => [m.key, m]));
    return text.replace(/@_user_\d+/g, (token) => {
        const mention = byKey.get(token);
        return mention ? `@${mentionDisplay(mention)}` : token;
    });
}

function parseLarkContent(
    messageType: string,
    rawContent: string,
    mentions: LarkMention[] = [],
): ContentItem[] {
    switch (messageType) {
        case 'text': {
            try {
                const c: TextContent = JSON.parse(rawContent);
                return [{ kind: 'text', text: applyMentionText(c.text, mentions) }];
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
                            items.push({
                                kind: 'text',
                                text: applyMentionText(node.text, mentions),
                            });
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
