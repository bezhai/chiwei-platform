// 投影一：飞书原生形态的正文。
//
// 与通用契约（inbound-message.ts）的区别只有一处，但很要紧：**@ 在这里是独立的
// 片段**。规则判定要回答"这条 @ 了谁"、消息落库要留下被 @ 的人的 id，一旦把 @
// 拍平成 "@张三" 这样的一串字，这两件事就都做不到了。
//
// 类型字面量（text / mention / image / sticker / media / file / audio /
// unsupported）是**已经写进历史消息记录的值**，改一个字面量就等于让旧消息读不出
// 来。领域里管视频叫 video，但落到这里必须仍是 media。

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
