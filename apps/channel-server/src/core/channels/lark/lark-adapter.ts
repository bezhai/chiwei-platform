// 飞书 channel adapter —— T1 四层契约下的第一个 adapter（行为不变的回归基线）。
//
// 这是飞书专有耦合的唯一收口处。im.message.receive_v1 / union_id / chat_id /
// challenge / verification_token 这些飞书字眼只允许出现在本文件内部；契约层
// （contracts.ts）和未来的 channel-server 主流程一律只见通用 InboundMessage /
// OutboundAdapter / AddressingDecision。
//
// 三件套：
//   LarkInboundAdapter   飞书事件 -> 通用 InboundMessage
//   LarkOutboundAdapter  通用发送/回复 -> 现有飞书 SDK 发送链路
//   LarkAddressingPolicy bot 命中判断，与现有 NeedRobotMention 逻辑等价

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
import {
    type AddressingDecision,
    type AddressingPolicy,
    type ContentItem,
    type InboundAdapter,
    type InboundMessage,
    type OutboundAdapter,
    type ThreadRef,
} from '../contracts';

export const LARK_CHANNEL = 'lark';

export class LarkInboundAdapter implements InboundAdapter {
    // 飞书回调握手：url_verification 事件原样回 challenge；其余（含真实消息
    // 事件）不是握手，返回 null 让上层继续走 verify/parse。这复刻飞书现状语义。
    handleHandshake(raw: unknown): unknown | null {
        const r = raw as { type?: string; challenge?: string };
        if (r && r.type === 'url_verification' && typeof r.challenge === 'string') {
            return { challenge: r.challenge };
        }
        return null;
    }

    // 飞书的回调安全靠 verification_token + encrypt_key 校验（解密 + token 比对）。
    // 该校验在飞书 webhook 入口（channel-proxy）完成，事件抵达本服务时已解密验证过。
    // 契约要求"没有签名机制的 channel 实现为恒 true 并说明为何安全"——这里同理：
    // 进到 adapter 的事件已过 channel-proxy 的 token+encrypt_key 校验，故恒 true 安全。
    verify(_raw: unknown): boolean {
        return true;
    }

    // parse 是纯转换：飞书原生事件 -> 通用 InboundMessage，零 I/O。内容映射
    // （parseLarkContent）、mention->addressing_hints、字段抽取全是同步的。现状
    // MessageTransferer.transfer 之所以 async，仅因为它额外调 Message.fromEvent
    // （走身份/DB），那一步不属于 parse 的职责（contract 把它留给下游）。所以
    // parse 保持与 InboundAdapter 契约一致的同步签名——T5 按接口类型调用时直接
    // 拿到消息本体，而不是 Promise。
    parse(raw: LarkReceiveMessage): InboundMessage | null {
        const event = raw;
        if (!event?.message || !event.message.message_id) return null;

        // 飞书现状里赤尾会处理图片/富文本/sticker/media/file/audio/合并转发/
        // 分享名片/未知类型——绝不能因为接 channel 契约就把它们当没收到。这里把
        // 飞书原生类型逐一映射到通用 ContentItem，映射口径与现状 MessageTransferer
        // / MessageContentUtils 逐字一致（飞书专有解析细节只在本文件内）。
        const content = parseLarkContent(
            event.message.message_type,
            event.message.content,
        );
        if (content.length === 0) return null;

        // 飞书 p2p -> direct，其余（group）-> group。下游需要的 is_direct / 旧
        // chat_type 由消费方从 conversation_scope 映射，语义不丢。
        const conversationScope = event.message.chat_type === 'p2p' ? 'direct' : 'group';

        // 飞书 mention -> addressing_hints。targetId 用 union_id 口径，与现有
        // MentionUtils.addMentions（mentions[].id.union_id）逐字一致，从而和
        // hasMention(getBotUnionId()) 的比对口径同源（见 LarkAddressingPolicy）。
        const mentions: LarkMention[] = event.message.mentions ?? [];
        const addressingHints = mentions.map((m) => ({ targetId: m.id.union_id! }));

        // 飞书出站现状是 replyMessage(message.messageId, content, replyInThread=true)
        // ——回复触发那条消息本身、且留在话题串内。所以入站消息自身就是回复锚点：
        // selfChannelMessageId 永远填这条消息的 message_id，inThread 永远 true，
        // 使 deliver/reply 路径下"回复触发消息且在线程内"零行为变化。root/parent
        // 仍带上，作为没有 self 锚点时的回退（如历史消息回放场景）。
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

        const receivedAt = Number(event.message.create_time) || 0;

        return {
            channel: LARK_CHANNEL,
            bot_name: event.app_id ?? '',
            channel_message_id: event.message.message_id,
            channel_chat_id: event.message.chat_id,
            channel_user_id: event.sender.sender_id?.union_id ?? 'unknown_sender',
            conversation_scope: conversationScope,
            thread_ref: threadRef,
            addressing_hints: addressingHints,
            content,
            received_at: receivedAt,
        };
    }
}

// 飞书 SDK 发送链路的最小注入面。adapter 只用到 send/reply 两个原子操作；
// 把它们作为依赖注入，默认走现有 @lark-client（懒加载 import，避免 adapter
// 模块在 import 期就把整条飞书 SDK 链拉进来——既是测试可注入点，也让飞书
// 重型耦合真正只在调用时发生）。
export interface LarkSendTransport {
    send(chatId: string, content: unknown, msgType: string): Promise<{ message_id?: string }>;
    reply(
        messageId: string,
        content: unknown,
        msgType: string,
        replyInThread: boolean,
    ): Promise<{ message_id?: string }>;
}

const defaultLarkTransport: LarkSendTransport = {
    async send(chatId, content, msgType) {
        const { send } = await import('@lark-client');
        return send(chatId, content, msgType);
    },
    async reply(messageId, content, msgType, replyInThread) {
        const { reply } = await import('@lark-client');
        return reply(messageId, content, msgType, replyInThread);
    },
};

export class LarkOutboundAdapter implements OutboundAdapter {
    private readonly transport: LarkSendTransport;

    constructor(transport: LarkSendTransport = defaultLarkTransport) {
        this.transport = transport;
    }

    // 包现有飞书纯文本发送链路（@lark-client send，msg_type=text）。行为、参数
    // 与现有 sendMsg 完全一致；返回飞书 message_id 作为 channel_message_id。
    async send(channelChatId: string, content: string): Promise<string> {
        const resp = await this.transport.send(channelChatId, { text: content }, 'text');
        return resp?.message_id ?? '';
    }

    // 包现有飞书纯文本回复链路（@lark-client reply，msg_type=text）。与现有
    // replyMessage(message.messageId, content, replyInThread) 行为一致：优先
    // 回复触发消息本身（selfChannelMessageId），并把 ThreadRef.inThread 透传成
    // 飞书 reply_in_thread。没有 self 锚点时回退到 parent/root（历史消息回放等
    // 无"触发消息自身"的场景）。
    async reply(threadRef: ThreadRef, content: string): Promise<string> {
        const target =
            threadRef.selfChannelMessageId ??
            threadRef.replyToChannelMessageId ??
            threadRef.rootChannelMessageId ??
            '';
        const replyInThread = threadRef.inThread === true;
        const resp = await this.transport.reply(
            target,
            { text: content },
            'text',
            replyInThread,
        );
        return resp?.message_id ?? '';
    }
}

// 飞书原生消息类型 -> 通用 ContentItem[]。映射口径与现状 MessageTransferer 的
// 各 *MessageContentFactory + MessageContentUtils 渲染逐字对齐：text/image/
// sticker/post/media/file/audio 各自的取值与解析失败占位串都照搬，merge_forward
// /share_chat/share_user/未知类型 -> unsupported（保留 original_type，绝不静默
// 丢弃）。飞书专有字段名（image_key/file_key/zh_cn 等）只在本函数内出现。
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
                {
                    kind: 'unsupported',
                    text: '[合并转发]',
                    meta: { original_type: 'merge_forward' },
                },
            ];
        case 'share_chat':
            return [
                {
                    kind: 'unsupported',
                    text: '[分享群名片]',
                    meta: { original_type: 'share_chat' },
                },
            ];
        case 'share_user':
            return [
                {
                    kind: 'unsupported',
                    text: '[分享个人名片]',
                    meta: { original_type: 'share_user' },
                },
            ];
        default:
            return [
                {
                    kind: 'unsupported',
                    text: `[${messageType}]`,
                    meta: { original_type: messageType },
                },
            ];
    }
}

export class LarkAddressingPolicy implements AddressingPolicy {
    // 与现有 NeedRobotMention 逻辑等价：
    //   NeedRobotMention = message.hasMention(getBotUnionId()) || message.isP2P()
    // 其中 isP2P() <=> conversation_scope === 'direct'（adapter 把 p2p 映射到
    // direct）；hasMention(botUnionId) <=> addressing_hints 里有 targetId 等于
    // botIdentity（addressing_hints 由 MentionUtils.addMentions 产出，正是
    // mentions[].id.union_id，与 hasMention 比对的 union_id 列表同源同口径）。
    // botIdentity 由调用方按 channel 取 bot 标识（飞书是 robot_union_id）传入，
    // policy 不自己读 context，保持与现有 getBotUnionId() 解耦。
    decide(msg: InboundMessage, botIdentity: string): AddressingDecision {
        if (msg.conversation_scope === 'direct') {
            return { respond: true, reason: 'direct conversation: bot always responds' };
        }
        const mentioned = msg.addressing_hints.some((h) => h.targetId === botIdentity);
        if (mentioned) {
            return {
                respond: true,
                reason: `bot ${botIdentity} mentioned in group conversation`,
            };
        }
        return {
            respond: false,
            reason: `group message without bot ${botIdentity} mention; not addressed to bot`,
        };
    }
}
