// 把用户刚说的那条消息重新拼成一条**发得出去**的飞书富文本。
//
//     入站正文片段 ──larkAtTaggedText──▶ 带 <at> 标签的一串字 ──echoPostContent──▶ PostContent
//                                            （也是复读的计数依据）
//
// 中间那一串字有两个用途，这解释了它为什么是两步而不是一步：它既是渲染的输入，也是
// **计数的输入** —— 复读比的是"这次说的跟上次说的一不一样"，比的就是它（拆分前
// 逐字相同：`renderLarkMentionText` 的结果先拿去算 md5，再拿去拼富文本）。
//
// ## 为什么复读不能走 markdown 那条路
//
// 出站主链路（赤尾的回复）把整段文字塞进一个 md 节点，飞书自己解析里面的格式。复读不
// 行：复读的内容是**用户原话**，`*强调*`、`# 标题`、`_下划线_` 会被 md 节点吃掉，复读
// 出来就跟原话不一样了。而且飞书自带表情在 post 里是独立的 emotion 节点，md 里塞不进
// 去。所以这条路自己走 text / at / emotion 三种节点。
//
// ## 表情查不到就当普通文字
//
// 发一个 emoji_type 对不上的 emotion 节点，飞书拒收的是**整条**消息 —— 一个不认识的
// 表情让整次复读消失，而且只在日志里留一条发送失败。降级成 `[原文]` 至少把话说出去了。

import type { LarkContentPart } from '../message/lark-content';
import type { LarkEmojiCatalog } from '../emoji/catalog';
import type { PostContent, PostNode } from '../outbound/post-content';

/** `[微笑]` 这样的表情占位。 */
const EMOJI_PATTERN = /\[([^\]]+)\]/g;

/** `<at user_id="on_xxx"></at>`，也就是上一步自己写出来的那个标签。 */
const AT_TAG_PATTERN = /<at user_id="([^"]+)"><\/at>/g;

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

/** 文本里出现的所有 `[xxx]`，按出现顺序，重复的不去掉。 */
function emojiTextsIn(text: string): string[] {
    return [...text.matchAll(EMOJI_PATTERN)].map((match) => match[1]!);
}

/**
 * 一段不含表情的文字 → 节点。`<at>` 标签切出 at 节点，其余成 text 节点。
 *
 * **纯空白的片段丢掉**（拆分前就是 `if (textBefore.trim())`）：节点之间那个用来隔开
 * 的空格没有意义，留着反而在飞书里渲染成多余的间隙。有实际内容时首尾空白照原样留着。
 */
function textAndMentions(text: string): PostNode[] {
    const nodes: PostNode[] = [];
    let cursor = 0;

    AT_TAG_PATTERN.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = AT_TAG_PATTERN.exec(text)) !== null) {
        if (match.index > cursor) {
            const before = text.slice(cursor, match.index);
            if (before.trim()) nodes.push({ tag: 'text', text: before });
        }
        nodes.push({ tag: 'at', user_id: match[1]! });
        cursor = match.index + match[0].length;
    }

    if (cursor < text.length) {
        const rest = text.slice(cursor);
        if (rest.trim()) nodes.push({ tag: 'text', text: rest });
    }

    return nodes;
}

/**
 * 带 `<at>` 标签的一串字 → 飞书富文本。
 *
 * 表情**一次问完**：一条消息里可能有好几个 `[xxx]`，逐个查就是逐个往返。
 */
export async function echoPostContent(
    catalog: Pick<LarkEmojiCatalog, 'emojisByText'>,
    text: string,
): Promise<PostContent> {
    const rows = await catalog.emojisByText(emojiTextsIn(text));
    const keyOf = new Map(rows.map((row) => [row.text, row.key]));

    const nodes: PostNode[] = [];
    let cursor = 0;

    EMOJI_PATTERN.lastIndex = 0;
    let match: RegExpExecArray | null;
    while ((match = EMOJI_PATTERN.exec(text)) !== null) {
        if (match.index > cursor) {
            nodes.push(...textAndMentions(text.slice(cursor, match.index)));
        }
        cursor = match.index + match[0].length;

        const emojiText = match[1]!;
        const key = keyOf.get(emojiText);
        // 查不到就当普通文字，理由见文件头。
        nodes.push(key ? { tag: 'emotion', emoji_type: key } : { tag: 'text', text: `[${emojiText}]` });
    }

    if (cursor < text.length) {
        nodes.push(...textAndMentions(text.slice(cursor)));
    }

    // 一个节点都产出不了（空串、纯空白）：飞书不收空 content，兜一个装着原文的
    // text 节点，发出去是一条看得见的消息而不是一次报错。
    if (nodes.length === 0) return { content: [[{ tag: 'text', text }]] };

    return { content: [nodes] };
}
