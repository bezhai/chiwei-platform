// 投影一：飞书原生形态的正文。
//
// 与通用契约（inbound-message.ts）的区别只有一处，但很要紧：**@ 在这里是独立的
// 片段**。规则判定要回答"这条 @ 了谁"、消息落库要留下被 @ 的人的 id，一旦把 @
// 拍平成 "@张三" 这样的一串字，这两件事就都做不到了。
//
// 类型字面量（text / mention / image / sticker / media / file / audio /
// unsupported）是**已经写进历史消息记录的值**，改一个字面量就等于让旧消息读不出
// 来。领域里管视频叫 video，但落到这里必须仍是 media。

import { TextUtils } from '@inner/shared';

import type { LarkInboundMessage, LarkSegment, LarkTextRun } from './parse-message';
import type { LarkMentionIndex } from './mentions';

export type LarkContentPart =
    | { type: 'text'; value: string }
    | {
          type: 'mention';
          value: string;
          meta: { channel_user_id?: string; bot_common_user_id?: string };
      }
    | { type: 'image'; value: string }
    | { type: 'sticker'; value: string }
    | {
          type: 'media';
          value: string;
          meta: { image_key?: string; file_name?: string; duration?: number };
      }
    | { type: 'file'; value: string; meta: { file_name?: string } }
    | { type: 'audio'; value: string; meta: { duration?: number } }
    | { type: 'unsupported'; value: string; meta: { original_type: string } };

function renderRun(run: LarkTextRun, mentions: LarkMentionIndex): LarkContentPart {
    if (run.kind === 'literal') return { type: 'text', value: run.text };

    const mention = mentions.byToken(run.token);
    // 正文里的占位符在 mentions 里查无此人：原样留成文字。猜一个名字比留着占位符
    // 更糟 —— 至少占位符一眼就能看出是哪里没对上。
    if (!mention) return { type: 'text', value: run.token };

    return {
        type: 'mention',
        value: mention.displayName,
        meta: {
            channel_user_id: mention.unionId,
            bot_common_user_id: mention.botCommonUserId,
        },
    };
}

function renderSegment(segment: LarkSegment, mentions: LarkMentionIndex): LarkContentPart[] {
    switch (segment.kind) {
        case 'text':
            return segment.runs.map((run) => renderRun(run, mentions));
        case 'image':
            return [{ type: 'image', value: segment.imageKey }];
        case 'sticker':
            return [{ type: 'sticker', value: segment.fileKey }];
        case 'video':
            return [
                {
                    type: 'media',
                    value: segment.fileKey,
                    meta: {
                        image_key: segment.imageKey,
                        file_name: segment.fileName,
                        duration: segment.duration,
                    },
                },
            ];
        case 'file':
            return [{ type: 'file', value: segment.fileKey, meta: { file_name: segment.fileName } }];
        case 'audio':
            return [{ type: 'audio', value: segment.fileKey, meta: { duration: segment.duration } }];
        case 'unsupported':
            return [
                {
                    type: 'unsupported',
                    value: segment.placeholder,
                    meta: { original_type: segment.originalType },
                },
            ];
    }
}

export function larkContentOf(
    message: LarkInboundMessage,
    mentions: LarkMentionIndex,
): LarkContentPart[] {
    return message.segments.flatMap((segment) => renderSegment(segment, mentions));
}

// ---------------------------------------------------------------------------
// 读这份正文
// ---------------------------------------------------------------------------
//
// 规则判定（`EqualText('余额')`、`ContainKeyword`、"只收纯文本"……）问的都是这几个
// 问题。**它们必须建在这份飞书原生片段上，不能建在通用契约的 content 上** ——
// 后者把 @ 内联回了文本，clearText 会变成含 "@赤尾" 的一串字，每条指令从此失配。
//
// 口径照拆分前的 MessageContentUtils 逐条重写：clearText 只认 text 片段、text()
// 把 mention 渲染成 "@显示名"、isTextOnly 用 every（所以空正文算纯文本 —— QQ 那侧
// 另外要求非空，两个渠道本来就不一致，这里按飞书那份走）。

/** 指令匹配用的正文：只取文字片段，去掉残留的 `@_user_N`，空白折成单空格。 */
export function larkClearText(parts: LarkContentPart[]): string {
    return TextUtils.clearText(
        parts
            .filter((part) => part.type === 'text')
            .map((part) => part.value)
            .join(''),
    );
}

/** 人在群里看到的那一串：@ 渲染成 "@显示名"，空白原样保留。 */
export function larkText(parts: LarkContentPart[]): string {
    return parts
        .filter((part) => part.type === 'text' || part.type === 'mention')
        .map((part) => (part.type === 'mention' ? `@${part.value}` : part.value))
        .join('');
}

/** clearText 再去掉 `[表情]` / `<标记>` 这类不是用户写的字的东西。 */
export function larkWithoutEmojiText(parts: LarkContentPart[]): string {
    return TextUtils.removeEmoji(larkClearText(parts));
}

export function larkIsTextOnly(parts: LarkContentPart[]): boolean {
    return parts.every((part) => part.type === 'text' || part.type === 'mention');
}

export function larkIsStickerOnly(parts: LarkContentPart[]): boolean {
    return parts.length === 1 && parts[0]!.type === 'sticker';
}

/** 没有表情包时是空串 —— 调用方按空串判断"这条没有"。 */
export function larkStickerKey(parts: LarkContentPart[]): string {
    return parts.find((part) => part.type === 'sticker')?.value ?? '';
}

/** 只有 image 片段算图片：视频的封面图在 media 片段的 meta 里，不是一张图。 */
export function larkImageKeys(parts: LarkContentPart[]): string[] {
    return parts.filter((part) => part.type === 'image').map((part) => part.value);
}

/**
 * 只有 file 片段算文件。
 *
 * 视频（media）和语音（audio）在飞书那侧同样是 file_key，但**不进文件轨** —— 附件缓存
 * 的两条轨按片段类型分流（见 attachments.ts），把视频塞进文件轨等于让「读小说」那类
 * 调用方从对象存储里读到一段 mp4。
 */
export function larkFileKeys(parts: LarkContentPart[]): string[] {
    return parts.filter((part) => part.type === 'file').map((part) => part.value);
}
