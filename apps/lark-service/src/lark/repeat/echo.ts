// 把用户刚说的那条消息拼回一串**带 `<at>` 标签的字**。
//
//     入站正文片段 ──larkAtTaggedText──▶ 带 <at> 标签的一串字 ──larkTextToPostContent──▶ PostContent
//                                            （也是复读的计数依据）
//
// 中间那一串字有两个用途，这解释了它为什么是两步而不是一步：它既是渲染的输入，也是
// **计数的输入** —— 复读比的是"这次说的跟上次说的一不一样"，比的就是它（拆分前
// 逐字相同：`renderLarkMentionText` 的结果先拿去算 md5，再拿去拼富文本）。
//
// 第二步跟赤尾的回复是同一个函数（../outbound/post-content.ts）：两边都是原话，都要
// 把 `[微笑]` 换成飞书表情、把 `*` `_` 原样保住。

import type { LarkContentPart } from '../message/lark-content';

/**
 * 正文片段 → 一串带 `<at>` 标签的字。
 *
 * mention 拿得到 union_id 就写成飞书认的标签，拿不到退回 `@显示名` —— 发一个
 * `user_id=""` 的标签飞书会拒收整条消息，而空名字会渲染成一个光秃秃的 "@"。
 *
 * 其余片段一律取 `value`：文字是文字本身，图片/表情包/文件是它们的 key。复读只在
 * 纯文字和纯表情包两种消息上触发（见 repeat.ts），所以后者实际走不到 —— 但拆分前
 * 这个函数就是无差别 map 的，照搬。
 */
export function larkAtTaggedText(parts: readonly LarkContentPart[]): string {
    return parts
        .map((part) => {
            if (part.type !== 'mention') return part.value;
            const unionId = part.meta.channel_user_id;
            if (typeof unionId === 'string' && unionId.length > 0) {
                return `<at user_id="${unionId}"></at>`;
            }
            return `@${part.value}`;
        })
        .join('');
}
