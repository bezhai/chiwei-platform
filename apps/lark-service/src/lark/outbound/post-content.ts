// 飞书富文本（post）的形状，以及从 markdown 到它的翻译。
//
// 这是渲染管线的**最后一步**，也是唯一知道飞书 post 长什么样的地方。前面两步
// （mention、图片）产出的都还是 markdown 文本，到这里才变成飞书的数据结构。
//
// 只定义出站真正会产出的两种节点。飞书的 post 还支持 a / at / emotion / code_block
// 等等，但出站一条都不产出：@ 在上一步就已经被写成 `<at user_id=...>` 标签留在文本
// 里，飞书会在 md 节点内部把它渲染成真正的 mention，不需要独立的 at 节点。多列一种
// 节点等于多留一条没人走的路。

/** 一段 markdown。飞书自己解析里面的加粗、斜体、链接、`<at>`。 */
export interface MdPostNode {
    tag: 'md';
    text: string;
}

/** 一张图。image_key 必须是飞书自己发的 key，外链和占位符都进不来（见下）。 */
export interface ImgPostNode {
    tag: 'img';
    image_key: string;
}

export type PostNode = MdPostNode | ImgPostNode;

/** content 是二维的：外层是行，内层是行内的节点。图片自成一行。 */
export interface PostContent {
    title?: string;
    content: PostNode[][];
}

/** markdown 的图片语法。alt 用 `.*?` 而不是 `[^\]]*` —— 名字里带 `]` 的 mention 已经被写进 alt 了。 */
const IMAGE_PATTERN = /!\[.*?\]\(([^)]+)\)/g;

/**
 * 还没被换成飞书 image_key 的注册表占位引用（`1.png` / `@1.png`）。
 *
 * 图片管线降级、或者根本没给注册表 id 时，这种引用会原样留到这一步。它不是飞书
 * image_key，硬发出去飞书会拒收**整条消息** —— 一张图挂掉变成一句话都发不出。
 */
const UNRESOLVED_REGISTRY_REF = /^@?\d+\.png$/;

function isExternalUrl(imageKey: string): boolean {
    return imageKey.startsWith('http://') || imageKey.startsWith('https://');
}

/**
 * 把 markdown 切成飞书的富文本。
 *
 * 图片是切分点：图片之间的文本各成一个 md 节点（首尾空白 trim 掉，空的不产出），
 * 图片各成一个 img 节点。
 *
 * **两类图片引用会被静默跳过**，跳过之后周围的文字照发：
 *   - 外链（http/https）—— 模型经常编造图片链接，飞书拿它渲染不出东西
 *   - 未解析的注册表占位符 —— 见 UNRESOLVED_REGISTRY_REF
 *
 * 全篇没有产出任何节点时补一个 md 节点，因为飞书不收空 content。补什么取决于**有没有
 * 匹配到过图片**：匹配到了却全被跳过，说明原文里全是图片语法，回退成原文等于把
 * `![...](https://...)` 这串源码直接摆给用户看，所以补空串。
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

        const imageKey = match[1];
        if (isExternalUrl(imageKey)) continue;
        if (UNRESOLVED_REGISTRY_REF.test(imageKey)) continue;
        content.push([{ tag: 'img', image_key: imageKey }]);
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
