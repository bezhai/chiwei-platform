// 把赤尾说的那段话、连同她要带的图，变成飞书认得的东西。
//
// 输入是一段 markdown（模型写出来的原话）加一串图片句柄。输出是飞书的富文本
// （PostContent）。三步，每步各自一个文件，这里只负责**把它们按正确的顺序串起来**。
//
//     1. mention   `@小明`  →  `<at user_id="on_xm">小明</at>`     群聊才做
//     2. post      markdown  →  若干 md 行                          总是做
//     3. pictures  句柄  →  现签、下载、上传  →  若干 img 行         有句柄才做
//
// ## 图从句柄来，不从正文来
//
// 上游的正文是一个对话模型自由生成的，没有任何原样保留的通道 —— 混在正文里的图片
// 引用必然被改写或丢掉，而且不报错。所以图有自己的字段（出站消息的
// `picture_file_names`，对象存储的永久句柄），第 2 步则把正文那条路彻底堵死：正文里
// 的图片引用一个都变不成 image_key（飞书认不出的 key 会让它拒收**整条消息**）。
//
// 图接在正文后面，各自成行。第 3 步只**追加**行，一个字都不改正文 —— 这也是它为什么
// 排在 mention 之后不再是个需要小心的问题。
//
// ## mention 必须看见赤尾原话
//
// mention 匹配的是她自己写下的名字。让别的步骤先改写正文，mention 面对的就不再是她
// 写的那段话：@ 可能凭空多出来、也可能被抹掉，两种都无声无息 —— 发出去的消息看着
// 通顺，只是 @ 错了人或者少了一个。所以它排第一。
//
// **不做的事**：本文件不认识出站队列的载荷长什么样。把载荷里的内容拼成一段 markdown、
// 把句柄取出来交给这里，是消费侧的事 —— 它知道分段、知道 content item 的种类，这里
// 只知道"一段话和几个句柄"。

import type { LarkMentionResolver } from './mentions';
import { markdownToPostContent, type PostContent } from './post-content';
import { larkPictureRows, type LarkPictureDeps } from './pictures';

/** 渲染这一段话需要知道的外部坐标。两项都可缺省，缺了就跳过对应的那一步。 */
export interface LarkRenderContext {
    /**
     * 这条消息发到哪个飞书群。
     *
     * **私聊不填。** 私聊里没有第三个人，@ 谁都渲染不成 mention，查一次群成员纯属白
     * 花一次查询。填了就当群聊处理。
     */
    mentionChatId?: string;

    /**
     * 这一段要带出去的图，值是对象存储的**永久句柄**（file_name），不是地址。
     *
     * 不填 / 空数组表示这一段不带图 —— 旧版 agent-service 发的消息（DLQ 里躺着的那些）
     * 就是这种，行为与加这个字段之前逐字一致，一次外部调用都不发生。
     */
    pictureFileNames?: readonly string[];
}

export type LarkPostRenderer = (
    markdown: string,
    ctx: LarkRenderContext,
) => Promise<PostContent>;

export interface LarkRenderDeps {
    /** 群 @ 解析。实现见 mentions.ts。 */
    mentions: LarkMentionResolver;
    /** 句柄 → 飞书 img 行要用到的三个协作者。实现见 pictures.ts。 */
    pictures: LarkPictureDeps;
}

export function createLarkPostRenderer(deps: LarkRenderDeps): LarkPostRenderer {
    return async (markdown, ctx) => {
        let text = markdown;

        // 顺序见文件头。mention 必须先看见原话。
        if (ctx.mentionChatId) {
            text = await deps.mentions(text, ctx.mentionChatId);
        }

        const post = markdownToPostContent(text);

        const fileNames = ctx.pictureFileNames ?? [];
        if (fileNames.length === 0) return post;

        // 只追加，不改正文。一张图挂了在这里已经降级成一行文字（见 pictures.ts），
        // 所以这一步永远不抛，她那句话照常送到。
        const pictures = await larkPictureRows(fileNames, deps.pictures);
        return { ...post, content: [...post.content, ...pictures] };
    };
}
