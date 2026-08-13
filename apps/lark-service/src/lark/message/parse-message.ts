// 把一条飞书消息事件读成结构化的飞书领域对象。**这是本服务里唯一一处解释
// `message.content` 的地方。**
//
// 为什么只有一处：拆分前飞书正文被解析了两遍 —— 一遍产出富对象喂给规则引擎，
// 一遍产出通用渠道契约喂给下游。两份 `switch (message_type)` 各自 JSON.parse、
// 各自写一套中文占位串，改一种消息类型要记得改两个地方，忘了就只有一半生效。
//
// 合并的办法不是"挑一个留下"，而是承认它们是**同一次解析的两种投影**：
//
//     飞书事件 ──parseLarkMessage──▶ LarkSegment[]  ─┬─▶ 飞书领域内容（mention 是独立片段）
//                                   （本文件）      └─▶ 通用渠道契约（mention 内联回文本）
//
// 关键在文本片段的表示：一段文本被 `@_user_N` 切成若干 run 之后**保留切口**，
// 谁需要独立的 mention 片段就展开 run，谁需要一整段文本就把 run 拼回去。切口只
// 算一次，两种形状都是它的确定性函数。
//
// 本文件是纯函数：不查库、不读 bot 目录、不认识"谁是机器人"。被 @ 的人叫什么名字
// 要查 bot 目录，那是 mentions.ts 的事 —— 拆开之后解析这一层的测试不需要任何桩。

import type {
    LarkAudioPayload,
    LarkFilePayload,
    LarkImagePayload,
    LarkMention,
    LarkMessageEvent,
    LarkPostPayload,
    LarkStickerPayload,
    LarkTextPayload,
    LarkVideoPayload,
} from './wire';

/** 一段文本被 `@_user_N` 切开之后的片段。 */
export type LarkTextRun =
    | { kind: 'literal'; text: string }
    | { kind: 'mention'; token: string };

/**
 * 消息正文的一段。飞书的一条消息可能由多段组成（富文本里文字和图片交替）。
 *
 * `video` 对应飞书的 `media` 消息类型 —— 那个名字含糊到看不出装的是什么，领域
 * 对象里叫它本来的东西。落库用的类型字面量仍是 `media`（见 lark-content.ts）。
 */
export type LarkSegment =
    | { kind: 'text'; runs: LarkTextRun[] }
    | { kind: 'image'; imageKey: string }
    | { kind: 'sticker'; fileKey: string }
    | { kind: 'video'; fileKey: string; imageKey?: string; fileName?: string; duration?: number }
    | { kind: 'file'; fileKey: string; fileName?: string }
    | { kind: 'audio'; fileKey: string; duration?: number }
    | { kind: 'unsupported'; placeholder: string; originalType: string };

/** 一条飞书消息在本服务里的领域形态。字段是事件里就有的事实，没有任何库里查来的东西。 */
export interface LarkInboundMessage {
    messageId: string;
    rootId?: string;
    parentId?: string;
    threadId?: string;
    chatId: string;
    chatType: string;
    messageType: string;
    createTime: string;
    appId?: string;
    sender: {
        unionId?: string;
        openId?: string;
        userId?: string;
    };
    /** 原样保留：谁被 @ 了是事实，叫什么名字是解释。 */
    mentions: LarkMention[];
    segments: LarkSegment[];
}

const MENTION_TOKEN = /@_user_\d+/g;

/**
 * 按 `@_user_N` 切开一段正文，切口保留成独立的 run。
 *
 * 空串会得到一个空 literal run 而不是空数组：正文为空是一条**合法**的消息，
 * 而"零个片段"在下游是"解析失败"的信号，两者不能混。
 */
export function splitMentionTokens(text: string): LarkTextRun[] {
    const runs: LarkTextRun[] = [];
    const pattern = new RegExp(MENTION_TOKEN.source, 'g');
    let cursor = 0;
    let match: RegExpExecArray | null;

    while ((match = pattern.exec(text)) !== null) {
        if (match.index > cursor) {
            runs.push({ kind: 'literal', text: text.slice(cursor, match.index) });
        }
        runs.push({ kind: 'mention', token: match[0] });
        cursor = match.index + match[0].length;
    }
    if (cursor < text.length) {
        runs.push({ kind: 'literal', text: text.slice(cursor) });
    }

    return runs.length > 0 ? runs : [{ kind: 'literal', text }];
}

/**
 * 解析不了的正文各自有一句中文占位串。它会原样落进消息记录、也会原样出现在赤尾
 * 读到的上下文里，所以字面量是**线上历史的一部分**，不能顺手改措辞。
 *
 * 占位串走 `literal` 而不是再过一次 splitMentionTokens：它不是用户写的字，不该
 * 被 mention 替换碰到。
 */
function placeholderText(placeholder: string): LarkSegment[] {
    return [{ kind: 'text', runs: [{ kind: 'literal', text: placeholder }] }];
}

function textSegment(text: string): LarkSegment {
    return { kind: 'text', runs: splitMentionTokens(text) };
}

function unsupported(placeholder: string, originalType: string): LarkSegment[] {
    return [{ kind: 'unsupported', placeholder, originalType }];
}

/**
 * 按 message_type 展开正文。飞书把 content 编码成 JSON 字符串，解析失败一律退到
 * 该类型的占位串 —— 绝不抛错，一条读不懂的消息不该让整条入站链断掉。
 */
function readSegments(messageType: string, rawContent: string): LarkSegment[] {
    switch (messageType) {
        case 'text':
            return read(rawContent, '[文本]', (payload: LarkTextPayload) => [
                textSegment(payload.text),
            ]);
        case 'image':
            return read(rawContent, '[图片]', (payload: LarkImagePayload) => [
                { kind: 'image', imageKey: payload.image_key },
            ]);
        case 'sticker':
            return read(rawContent, '[表情包]', (payload: LarkStickerPayload) => [
                { kind: 'sticker', fileKey: payload.file_key },
            ]);
        case 'post':
            return read(rawContent, '[富文本]', (payload: LarkPostPayload) => {
                const segments: LarkSegment[] = [];
                for (const row of payload.content) {
                    for (const node of row) {
                        // 一个 text 节点 = 一段。**不与相邻节点合并** —— 飞书按节点
                        // 分行分样式，合并会改变下游看到的片段数量。
                        if (node.tag === 'text' && node.text) {
                            segments.push(textSegment(node.text));
                        } else if (node.tag === 'img' && node.image_key) {
                            segments.push({ kind: 'image', imageKey: node.image_key });
                        }
                    }
                }
                // 整条富文本里没有一个能渲染的节点（纯 @、纯链接、纯代码块……）：
                // 退到占位串而不是产出零片段。
                return segments.length > 0 ? segments : placeholderText('[富文本]');
            });
        case 'media':
            return read(rawContent, '[视频]', (payload: LarkVideoPayload) => [
                {
                    kind: 'video',
                    fileKey: payload.file_key,
                    imageKey: payload.image_key,
                    fileName: payload.file_name,
                    duration: payload.duration,
                },
            ]);
        case 'file':
            return read(rawContent, '[文件]', (payload: LarkFilePayload) => [
                { kind: 'file', fileKey: payload.file_key, fileName: payload.file_name },
            ]);
        case 'audio':
            return read(rawContent, '[语音]', (payload: LarkAudioPayload) => [
                { kind: 'audio', fileKey: payload.file_key, duration: payload.duration },
            ]);
        case 'merge_forward':
            return unsupported('[合并转发]', 'merge_forward');
        case 'share_chat':
            return unsupported('[分享群名片]', 'share_chat');
        case 'share_user':
            return unsupported('[分享个人名片]', 'share_user');
        default:
            // 认得出是一条消息、但本通道不渲染。originalType 必须留下，否则
            // "收到了但没处理"完全不可观测。
            return unsupported(`[${messageType}]`, messageType);
    }
}

function read<T>(
    rawContent: string,
    placeholder: string,
    build: (payload: T) => LarkSegment[],
): LarkSegment[] {
    try {
        return build(JSON.parse(rawContent) as T);
    } catch (error) {
        console.error(`[lark-parse] cannot read ${placeholder} payload:`, error);
        return placeholderText(placeholder);
    }
}

/**
 * 事件 → 领域对象。没有 message_id 的事件返回 null（不是消息，或者报文残缺），
 * 调用方据此跳过；解析不了的正文不返回 null，而是留下占位串继续往下走。
 */
export function parseLarkMessage(event: LarkMessageEvent): LarkInboundMessage | null {
    const message = event?.message;
    if (!message?.message_id) return null;

    return {
        messageId: message.message_id,
        rootId: message.root_id,
        parentId: message.parent_id,
        threadId: message.thread_id,
        chatId: message.chat_id,
        chatType: message.chat_type,
        messageType: message.message_type,
        createTime: message.create_time,
        appId: event.app_id,
        sender: {
            unionId: event.sender?.sender_id?.union_id,
            openId: event.sender?.sender_id?.open_id,
            userId: event.sender?.sender_id?.user_id,
        },
        mentions: message.mentions ?? [],
        segments: readSegments(message.message_type, message.content),
    };
}
