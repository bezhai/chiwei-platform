// 把赤尾说的那段话变成飞书认得的东西。
//
// 输入是 markdown 文本 —— 模型写出来的原话。输出是飞书的富文本（PostContent）。
// 中间三步，每步各自一个文件，这里只负责**把它们按正确的顺序串起来**。
//
//     1. mention   `@小明`  →  `<at user_id="on_xm">小明</at>`     群聊才做
//     2. images    `![x](1.png)`  →  `![x](img_v3_abc)`           有注册表才做
//     3. post      markdown  →  PostContent                       总是做
//
// ## 顺序是不变量，不是风格
//
// 前两步都在重写同一段文本，而且都不是局部替换：图片那一步会把整个 `![...](...)`
// 区间换掉 —— 成功换成新的 image_key，失败换成"（图片 1.png 不可用）"这类中文。
//
// mention 匹配的是**赤尾自己写下的名字**。让图片先跑，mention 面对的就不再是她写的
// 那段话，而是一段被别的步骤加工过的文本：@ 可能凭空多出来（降级文案里正好有人名）、
// 也可能被抹掉（名字落在被替换的区间里）。两种都无声无息 —— 发出去的消息看着通顺，
// 只是 @ 错了人或者少了一个。
//
// 反过来（mention 先跑）没有这个问题：mention 只往文本里插 `<at ...>` 标签，而标签
// 里不含图片语法，改不动第二步要认的东西。
//
// **不做的事**：本文件不认识出站队列的载荷长什么样。把载荷里的内容拼成一段 markdown
// 是消费侧的事 —— 它知道分段、知道 content item 的种类，这里只知道"一段话"。

import type { LarkImageDeps } from './images';
import { resolveImageReferences } from './images';
import type { LarkMentionResolver } from './mentions';
import { markdownToPostContent, type PostContent } from './post-content';

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
     * 图片注册表的 id，也就是这条消息的**全局** id。
     *
     * 不填表示这次出站没有可解析的图片来源，正文里的占位引用会被最后一步静默跳过。
     */
    imageRegistryId?: string;
}

export type LarkPostRenderer = (
    markdown: string,
    ctx: LarkRenderContext,
) => Promise<PostContent>;

export interface LarkRenderDeps {
    /** 群 @ 解析。实现见 mentions.ts。 */
    mentions: LarkMentionResolver;
    /** 图片引用解析要用的两个协作者。实现见 images.ts。 */
    images: LarkImageDeps;
}

export function createLarkPostRenderer(deps: LarkRenderDeps): LarkPostRenderer {
    return async (markdown, ctx) => {
        let text = markdown;

        // 顺序见文件头。调换这两行会让 render.test.ts 的「渲染顺序不变量」转红。
        if (ctx.mentionChatId) {
            text = await deps.mentions(text, ctx.mentionChatId);
        }
        text = await resolveImageReferences(text, ctx.imageRegistryId, deps.images);

        return markdownToPostContent(text);
    };
}
