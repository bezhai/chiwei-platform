// 投影二：通用渠道契约。这是本服务对外说的话 —— 出了 lark-service，没有人再
// 需要认识 `@_user_1`、`om_xxx`、`p2p` 这些飞书说法。
//
// 与飞书原生投影（lark-content.ts）的唯一实质差别：契约里没有"mention"这种片段，
// 所以 @ 要内联回文本，并且**一段源文本仍只产出一个 item** —— 一段文字被 @ 切成
// 三个 run，拼回去还是一条文字，不是三条。被 @ 的人不会因此丢失，他们在
// addressing_hints 里。

import type { AddressingHint, ContentItem, InboundMessage, ThreadRef } from '@inner/shared/channel';

import { LARK_CHANNEL } from '../channel';
import type { LarkMentionIndex } from './mentions';
import type { LarkInboundMessage, LarkSegment } from './parse-message';

function inlineText(segment: Extract<LarkSegment, { kind: 'text' }>, mentions: LarkMentionIndex) {
    return segment.runs
        .map((run) => {
            if (run.kind === 'literal') return run.text;
            const mention = mentions.byToken(run.token);
            // 查无此人就把占位符原样留在文本里，跟飞书原生投影同一个取向：不猜。
            return mention ? `@${mention.displayName}` : run.token;
        })
        .join('');
}

function toContentItem(segment: LarkSegment, mentions: LarkMentionIndex): ContentItem {
    switch (segment.kind) {
        case 'text':
            return { kind: 'text', text: inlineText(segment, mentions) };
        case 'image':
            return { kind: 'image', key: segment.imageKey };
        case 'sticker':
            return { kind: 'sticker', key: segment.fileKey };
        case 'video':
            // 契约里没有"视频"，视频和文件都是"可下载附件"。原始类型留在 meta，
            // 让本渠道的出站/媒体轨还能分辨。
            return {
                kind: 'file',
                key: segment.fileKey,
                meta: {
                    image_key: segment.imageKey,
                    file_name: segment.fileName,
                    duration: segment.duration,
                    lark_type: 'media',
                },
            };
        case 'file':
            return {
                kind: 'file',
                key: segment.fileKey,
                meta: { file_name: segment.fileName, lark_type: 'file' },
            };
        case 'audio':
            return { kind: 'audio', key: segment.fileKey, meta: { duration: segment.duration } };
        case 'unsupported':
            return {
                kind: 'unsupported',
                text: segment.placeholder,
                meta: { original_type: segment.originalType },
            };
    }
}

/**
 * 回复锚点。飞书出站现状是"回复触发的那条消息本身、并留在话题串内"，所以入站
 * 消息自己永远是锚点；parent / root 一并带上作为回退锚点。
 */
function threadRefOf(message: LarkInboundMessage): ThreadRef {
    const thread: ThreadRef = {
        selfChannelMessageId: message.messageId,
        inThread: true,
    };
    if (message.parentId) thread.replyToChannelMessageId = message.parentId;
    if (message.rootId) thread.rootChannelMessageId = message.rootId;
    return thread;
}

export function inboundMessageOf(
    message: LarkInboundMessage,
    mentions: LarkMentionIndex,
): InboundMessage {
    const addressingHints: AddressingHint[] = mentions.all.map((mention) => ({
        targetId: mention.unionId!,
    }));

    return {
        channel: LARK_CHANNEL,
        // 下游按飞书 app_id 认这条消息属于哪个 bot。字段名是契约层的通用叫法，
        // 装的是渠道内的 id —— 拆分不改这个口径。
        bot_name: message.appId ?? '',
        channel_message_id: message.messageId,
        channel_chat_id: message.chatId,
        // 渠道内的发送者 id 用 open_id：union_id 是租户维度的，出了本渠道也没人用。
        channel_user_id: message.sender.openId ?? 'unknown_sender',
        conversation_scope: message.chatType === 'p2p' ? 'direct' : 'group',
        thread_ref: threadRefOf(message),
        addressing_hints: addressingHints,
        content: message.segments.map((segment) => toContentItem(segment, mentions)),
        // 飞书的 create_time 是毫秒时间戳字符串。读不成数就记 0，而不是让 NaN
        // 流到下游 —— NaN 会让"这条消息什么时候到的"永远比不出大小。
        received_at: Number(message.createTime) || 0,
    };
}
