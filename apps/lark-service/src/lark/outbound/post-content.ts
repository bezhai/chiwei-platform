// 飞书富文本（post）的形状，以及从一段飞书口径的文字到它的翻译。
//
// 这是唯一知道飞书 post 长什么样的地方。产出它的有两条路，正文走的是**同一个**函数
// （larkTextToPostContent，本文件下半段）：
//
//   * **赤尾的回复**（render.ts）。@ 在上一步已经被写成 `<at user_id=...>名字</at>`。
//     她要带的图不从正文来：img 节点由 pictures.ts 产出、由 render.ts 接在正文后面。
//   * **复读**（../repeat/echo.ts 拼出带 `<at>` 标签的原话，../repeat/repeat.ts 发）。
//
// ## 为什么正文不用 md 节点
//
// 飞书的表情在 post 里是独立的 emotion 节点，而 md 节点「会独占一个或多个段落，不能
// 与其他标签在同一行」（飞书文档原文）—— 用 md 就不可能让 `哈哈[微笑]` 里的表情
// 出现在句中。她学着群里的人写 `[白眼]`（飞书 text 消息里表情本来就是这么编码的），
// 发出去却是一串字，就是这个原因。
//
// 而正文两边都是原话，md 的格式解析对它们只有坏处：颜表情 `_(´ཀ`」 ∠)_` 里的 `_`
// 和反引号会被当成斜体、代码吃掉。她的回复里从来不写真正的 markdown。
//
// 每种节点都有人真的产出，列在这里的不是"飞书还支持什么"（它还有 md / a / media /
// code_block），是**本服务真的会发出去什么**。

import type { LarkEmojiCatalog, LarkEmojiRow } from '../emoji/catalog';

/**
 * 一张图。image_key 必须是飞书自己发的 key —— 飞书认不出它就拒收**整条消息**。
 *
 * 唯一的产出方是 pictures.ts（那里的 key 刚从一次真实的上传拿回来）。正文永远变不出
 * 这个节点：larkTextToPostContent 只产出 text / at / emotion，正文里模型自己写的
 * `![x](y)` 就是一串字。
 */
export interface ImgPostNode {
    tag: 'img';
    image_key: string;
}

/** 一段**不解析格式**的纯文字。原话里的 markdown 符号、颜表情靠它原样保住。 */
export interface TextPostNode {
    tag: 'text';
    text: string;
}

/** 一个 @。user_id 这里放的是飞书的 union_id。 */
export interface AtPostNode {
    tag: 'at';
    user_id: string;
}

/** 一个飞书自带表情。emoji_type 是表情 key（`SMILE`），不是它显示的文本（`微笑`）。 */
export interface EmotionPostNode {
    tag: 'emotion';
    emoji_type: string;
}

export type PostNode =
    | ImgPostNode
    | TextPostNode
    | AtPostNode
    | EmotionPostNode;

/** content 是二维的：外层是行，内层是行内的节点。图片自成一行。 */
export interface PostContent {
    title?: string;
    content: PostNode[][];
}

/**
 * 一段文字里的记号：`<at>` 标签，或者 `[微笑]` 这样的表情占位。
 *
 * **at 排在前面**：名字里带 `[..]` 的 mention 必须整个被认成 at，不能被表情那一支
 * 从中间切开。表情那一支反过来不许跨过 `<` `>` —— 飞书的表情名里没有尖括号，而
 * `[给<at ...>小明</at>看]` 这种方括号包着 at 标签的写法要让 at 标签自己被认出来，
 * 否则它会被当成一个查不到的表情，以原文漏给用户。
 */
const TOKEN_PATTERN = /<at user_id="([^"]+)">.*?<\/at>|\[([^[\]<>]+)\]/g;

/** 文本里出现的所有 `[xxx]`（at 标签里面的不算），按出现顺序，重复的不去掉。 */
function emojiTextsIn(text: string): string[] {
    return [...text.matchAll(TOKEN_PATTERN)].flatMap((match) => (match[2] ? [match[2]] : []));
}

/**
 * 一行（不是空白行）→ 节点。
 *
 * 记号之间的空格照原样留成 text 节点：它是原话的一部分，`[foo] [bar]` 丢掉中间那个
 * 就成了 `[foo][bar]`。飞书自己的消息里也是这样带着空格的（`" 🦞"`）。
 */
function lineNodes(line: string, keyOf: ReadonlyMap<string, string>): PostNode[] {
    const nodes: PostNode[] = [];
    const pushText = (text: string) => {
        if (text) nodes.push({ tag: 'text', text });
    };

    let cursor = 0;
    for (const match of line.matchAll(TOKEN_PATTERN)) {
        pushText(line.slice(cursor, match.index));
        cursor = match.index + match[0].length;

        const [token, unionId, emojiText] = match;
        if (unionId) {
            nodes.push({ tag: 'at', user_id: unionId });
            continue;
        }
        // 查不到就当普通文字：发一个 emoji_type 对不上的 emotion 节点，飞书拒收的是
        // **整条**消息 —— 一个不认识的 `[0, +∞]` 让她整句话发不出去。
        const key = keyOf.get(emojiText!);
        nodes.push(key ? { tag: 'emotion', emoji_type: key } : { tag: 'text', text: token });
    }
    pushText(line.slice(cursor));

    return nodes;
}

async function emojisIn(
    catalog: Pick<LarkEmojiCatalog, 'emojisByText'>,
    text: string,
): Promise<readonly LarkEmojiRow[]> {
    const texts = emojiTextsIn(text);
    if (texts.length === 0) return [];
    try {
        return await catalog.emojisByText(texts);
    } catch (error) {
        console.error('[lark-post] emoji lookup failed, sending [xxx] as plain text:', error);
        return [];
    }
}

/**
 * 一段飞书口径的文字 → 飞书富文本。
 *
 * 「飞书口径」指 @ 已经是 `<at user_id="...">` 标签（里面写不写名字都行），表情是
 * `[微笑]` 这样的显示文本 —— 飞书 text 消息里表情本来就是这么编码的。
 *
 * **一行一个段落，空行不占段落**：飞书自己把多行消息编码成这个样子（bot 发出去的
 * md 回流回来也是这个形状）。
 *
 * 表情**一次问完**：一条消息里可能有好几个 `[xxx]`，逐个查就是逐个往返；一个都没有
 * 就不查。
 *
 * **查不了就一个都不换**，`[xxx]` 原样当文字发出去，绝不抛。表情是点缀，跟图一样
 * （见 pictures.ts）：出站那侧在渲染之前就已经把这次发送当成"可能到了飞书"，这里一抛，
 * 她那句话会被当成发送失败吞掉，一个字都到不了。
 */
export async function larkTextToPostContent(
    catalog: Pick<LarkEmojiCatalog, 'emojisByText'>,
    text: string,
): Promise<PostContent> {
    const keyOf = new Map((await emojisIn(catalog, text)).map((row) => [row.text, row.key]));

    const content = text
        .split('\n')
        .filter((line) => line.trim())
        .map((line) => lineNodes(line, keyOf));

    // 一个节点都产出不了（空串、纯空白）：飞书不收空 content，兜一个装着原文的
    // text 节点，发出去是一条看得见的消息而不是一次报错。
    return { content: content.length > 0 ? content : [[{ tag: 'text', text }]] };
}
