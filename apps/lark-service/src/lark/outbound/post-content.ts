// 飞书富文本（post）的形状，以及从 markdown 到它的翻译。
//
// 这是唯一知道飞书 post 长什么样的地方。产出它的有两条路，各自走到不同的节点：
//
//   * **赤尾的回复**（markdownToPostContent，本文件下半段）只走 md。@ 在渲染管线的
//     上一步就已经被写成 `<at user_id=...>` 标签留在 markdown 里，飞书会在 md 节点
//     内部把它渲染成真正的 mention，所以这条路不需要独立的 at 节点。她要带的图不从
//     正文来：img 节点由 pictures.ts 产出、由 render.ts 接在正文后面（理由见下）。
//   * **复读**（../repeat/echo.ts）走 text + at + emotion。它不能用 md：复读的内容是
//     用户原话，里面的 `*` `_` `#` 会被 md 节点当成格式吃掉，复读出来就跟原话不一样了。
//     表情也一样 —— 飞书的表情在 post 里是独立的 emotion 节点，md 里塞不进去。
//
// 每种节点都有人真的产出，列在这里的不是"飞书还支持什么"（它还有 a / media /
// code_block），是**本服务真的会发出去什么**。

/** 一段 markdown。飞书自己解析里面的加粗、斜体、链接、`<at>`。 */
export interface MdPostNode {
    tag: 'md';
    text: string;
}

/**
 * 一张图。image_key 必须是飞书自己发的 key —— 飞书认不出它就拒收**整条消息**。
 *
 * 唯一的产出方是 pictures.ts（那里的 key 刚从一次真实的上传拿回来）。正文永远变不出
 * 这个节点，见下面 markdownToPostContent 的注释。
 */
export interface ImgPostNode {
    tag: 'img';
    image_key: string;
}

/** 一段**不解析格式**的纯文字。复读用它保住用户原话里的 markdown 符号。 */
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
    | MdPostNode
    | ImgPostNode
    | TextPostNode
    | AtPostNode
    | EmotionPostNode;

/** content 是二维的：外层是行，内层是行内的节点。图片自成一行。 */
export interface PostContent {
    title?: string;
    content: PostNode[][];
}

/** markdown 的图片语法。alt 用 `.*?` 而不是 `[^\]]*` —— 名字里带 `]` 的 mention 已经被写进 alt 了。 */
const IMAGE_PATTERN = /!\[.*?\]\([^)]*\)/g;

/**
 * 把 markdown 切成飞书的富文本。
 *
 * 图片语法是切分点：它之间的文本各成一个 md 节点（首尾空白 trim 掉，空的不产出）。
 *
 * ## 正文里的图片引用**一律丢掉**，一个都不变成 img 节点
 *
 * 飞书的 image_key 只可能来自我们自己那次上传（见 pictures.ts）—— 而这一步的输入是
 * 赤尾说的那段话，由一个对话模型自由生成，里面出现的任何 `![x](y)` 都是它自己写的：
 * 编的外链、编的文件名、编的一串看着像 key 的字符。把这种东西当 image_key 发出去，
 * 飞书**拒收整条消息**，症状不是少一张图，是她那句话一个字都发不出去。
 *
 * 从"挡掉几种已知的坏写法"改成"一个都不认"，是因为前者是可枚举白名单的反面：认不出
 * 的新写法默认放行，而放行一次的代价是整条消息。图有自己的结构化通道
 * （`picture_file_names` → pictures.ts → 附在正文之后的 img 行），正文这条路不需要
 * 存在。
 *
 * 丢掉之后周围的文字照发。全篇没有产出任何节点时补一个 md 节点，因为飞书不收空
 * content。补什么取决于**有没有匹配到过图片**：匹配到了却全被丢掉，说明原文里全是
 * 图片语法，回退成原文等于把 `![...](...)` 这串源码直接摆给用户看，所以补空串。
 */
export function markdownToPostContent(markdown: string): PostContent {
    const content: PostNode[][] = [];
    let lastIndex = 0;
    let match: RegExpExecArray | null;

    IMAGE_PATTERN.lastIndex = 0;
    while ((match = IMAGE_PATTERN.exec(markdown)) !== null) {
        if (match.index > lastIndex) {
            const text = markdown.slice(lastIndex, match.index).trim();
            if (text) content.push([{ tag: 'md', text }]);
        }
        lastIndex = match.index + match[0].length;
    }

    if (lastIndex < markdown.length) {
        const text = markdown.slice(lastIndex).trim();
        if (text) content.push([{ tag: 'md', text }]);
    }

    if (content.length === 0) {
        content.push([{ tag: 'md', text: lastIndex > 0 ? '' : markdown }]);
    }

    return { content };
}
