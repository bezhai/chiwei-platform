// 飞书出站能力端口实现（B3）。整个仓库飞书出站 SDK 调用唯一出现的地方：
// 把现状 chat-response-worker / recall-worker inline 的飞书富文本出站管线
// （@N.png 图片引用 → 查注册表 + 下载 + 上传飞书拿 image_key、@用户名 → 飞书
// mention 标记、markdown → 飞书 PostContent、send/reply/delete）收进这里，
// 实现 core 定义的 OutboundCapabilities 端口。
//
// 控制方向：worker（core 边缘）只产出平台无关 ContentItem[]（AI 原始 markdown
// 文本）+ RenderContext（图片注册表全局 id / mention 会话 id），本模块在插件内
// 把它渲染成飞书 PostContent 再发。worker 不再 import 任何飞书 SDK。
//
// 飞书 SDK / redis / DB 这些 I/O 协作者全部由 LarkOutboundDeps 注入：生产默认
// 接现有 @lark/basic/message + @lark-client + redis + 插件内 mention 解析（见
// defaultLarkOutboundDeps），单测注入 spy 验证渲染口径与现状逐字一致。

import type {
    CommonMessageResolveInput,
    ConversationRef,
    MessageRef,
    OutboundMessageRecordInput,
    OutboundCapabilities,
    OutboundTargetResolveInput,
    RenderContext,
} from '@core/ports/channel-plugin';
import type { ContentItem, ThreadRef } from '@core/channels/contracts';
import type { PostContent } from 'types/content-types';
import { markdownToPostContent } from '@core/services/message/post-content-processor';
import { larkCredentials } from '@core/services/bot/lark-credentials';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { storeLarkOutboundMessage } from './common-projector';
import {
    resolveLarkMessageRef,
    reverseResolveOutbound,
} from './outbound-reverse-resolve';

// markdown 里的 @N.png 图片占位引用（与现状 chat-response-worker 同一正则）。
const IMAGE_REF_PATTERN = /!\[([^\]]*)\]\(@?(\d+\.png)\)/g;

// 飞书出站所需的 I/O 协作者。全部注入 → 渲染逻辑可测、飞书重耦合只在 default。
export interface LarkOutboundDeps {
    // 飞书 SDK：新发 / 回复 / 撤回。
    send(chatId: string, content: PostContent): Promise<{ message_id?: string }>;
    reply(
        messageId: string,
        content: PostContent,
        replyInThread: boolean,
    ): Promise<{ message_id?: string }>;
    deleteMessage(messageId: string): Promise<unknown>;
    // 飞书 SDK：上传图片，返回 image_key。
    uploadImage(image: Buffer): Promise<{ image_key?: string } | undefined>;
    // 图片注册表（redis hgetall）：filename → 外链 url。
    getImageRegistry(key: string): Promise<Record<string, string> | null | undefined>;
    // 群 mention 解析（DB）：@名字 → 飞书 <at user_id>。
    resolveMentionsForGroup(content: string, chatId: string): Promise<string>;
    // 下载外链图片为 bytes。
    fetchImage(url: string): Promise<Buffer>;
}

// 把 ContentItem[] 取出 worker 传入的 AI 原始 markdown 文本。B3 阶段 content 只
// 承载一个 text 片段（决策一：富内容渲染在插件内做，content 不预拆图片段）；
// 这里把所有 text 片段拼起来兜底，非 text 片段当前 worker 出站不会产生。
function extractMarkdown(content: ContentItem[]): string {
    return content
        .map((c) => (c.kind === 'text' || c.kind === 'unsupported' ? c.text : ''))
        .join('');
}

// reply 的回复锚点：优先回复触发消息本身，回退 parent/root。三者都空 = 装配出错，
// fail-loud，绝不把回复发到空目标（设计文档「禁止静默丢弃」的出站对偶）。
function resolveReplyAnchor(thread: ThreadRef): string {
    const anchor =
        thread.selfChannelMessageId ??
        thread.replyToChannelMessageId ??
        thread.rootChannelMessageId;
    if (!anchor) {
        throw new Error(
            'lark reply called with a ThreadRef that has no usable anchor ' +
                '(self/replyTo/root all empty); refusing to reply to an empty target ' +
                '(fail-loud, no wrong-send)',
        );
    }
    return anchor;
}

// 把 AI markdown 渲染成飞书 PostContent：先 @用户名 mention 解析（群聊），再
// @N.png 图片引用解析（查注册表 → 下载 → 上传飞书 → 替换 image_key），最后
// markdown → PostContent。与现状 chat-response-worker 的 resolve→send 段逐字等价。
async function renderToPost(
    deps: LarkOutboundDeps,
    content: ContentItem[],
    ctx: RenderContext,
): Promise<PostContent> {
    let markdown = extractMarkdown(content);

    // 群聊把 @用户名 替换为飞书 <at>。私聊（resolveMentions=false）跳过——与现状
    // worker `is_p2p ? content : resolveMentionsForGroup(...)` 一致。mention 必须
    // 先于 image 解析（与现状 worker 顺序逐字一致）：先把 @名字 翻成 <at>，再处理
    // 图片占位，避免 @名字 落在图片 alt/url 区间被误改。
    if (ctx.resolveMentions !== false && ctx.groupConversationId) {
        markdown = await deps.resolveMentionsForGroup(markdown, ctx.groupConversationId);
    }

    markdown = await resolveImageReferences(deps, markdown, ctx.imageRegistryId);

    return markdownToPostContent(markdown);
}

// @N.png 图片引用解析。注册表必须用【全局 imageRegistryId】查（产出图片的上游用
// 它注册），绝不能用飞书裸 id —— 否则注册表必 miss、图片被静默吞掉。
async function resolveImageReferences(
    deps: LarkOutboundDeps,
    markdown: string,
    imageRegistryId: string | undefined,
): Promise<string> {
    const matches = [...markdown.matchAll(IMAGE_REF_PATTERN)];
    if (matches.length === 0) return markdown;

    // 有图片引用但没给注册表 id：无从解析，原样返回（markdownToPostContent 会
    // 跳过未解析的 N.png，与现状「registry miss 原样返回」行为一致）。
    if (!imageRegistryId) return markdown;

    const registry = await deps.getImageRegistry(`image_registry:${imageRegistryId}`);
    if (!registry || Object.keys(registry).length === 0) {
        console.warn(
            `[lark-outbound] no image registry for id=${imageRegistryId}; leaving refs unresolved`,
        );
        return markdown;
    }

    let result = markdown;
    const CONCURRENCY = 5;
    for (let i = 0; i < matches.length; i += CONCURRENCY) {
        const batch = matches.slice(i, i + CONCURRENCY);
        const replacements = await Promise.all(
            batch.map((m) => resolveSingleImage(deps, m, registry)),
        );
        for (const { fullMatch, replacement } of replacements) {
            result = result.replace(fullMatch, replacement);
        }
    }
    return result;
}

async function resolveSingleImage(
    deps: LarkOutboundDeps,
    match: RegExpMatchArray,
    registry: Record<string, string>,
): Promise<{ fullMatch: string; replacement: string }> {
    const fullMatch = match[0];
    const alt = match[1];
    const filename = match[2];

    const url = registry[filename];
    if (!url) {
        console.warn(`[lark-outbound] image ${filename} not in registry`);
        return { fullMatch, replacement: `(图片 ${filename} 不可用)` };
    }

    try {
        const buffer = await deps.fetchImage(url);
        const uploaded = await deps.uploadImage(buffer);
        const imageKey =
            (uploaded as { image_key?: string; data?: { image_key?: string } } | undefined)
                ?.image_key ??
            (uploaded as { data?: { image_key?: string } } | undefined)?.data?.image_key;
        if (!imageKey) {
            console.error(`[lark-outbound] upload failed for ${filename}`);
            return { fullMatch, replacement: `(图片 ${filename} 上传失败)` };
        }
        return { fullMatch, replacement: `![${alt}](${imageKey})` };
    } catch (e) {
        console.error(`[lark-outbound] error resolving ${filename}:`, e);
        return { fullMatch, replacement: `(图片 ${filename} 处理失败)` };
    }
}

// 工厂：注入 deps，返回 OutboundCapabilities。生产用 defaultLarkOutboundDeps，
// 单测注入 spy。
export function createLarkOutboundCapabilities(
    deps: LarkOutboundDeps,
): OutboundCapabilities {
    return {
        async resolveOutboundTarget(input: OutboundTargetResolveInput) {
            const refs = await reverseResolveOutbound({
                commonMessageId: input.commonMessageId,
                commonConversationId: input.commonConversationId,
                commonRootMessageId: input.commonRootMessageId,
            });
            return {
                message: { channelId: refs.channelMessageId },
                conversation: { channelId: refs.channelChatId },
                rootMessage: refs.channelRootId
                    ? { channelId: refs.channelRootId }
                    : undefined,
            };
        },

        async resolveMessageRef(input: CommonMessageResolveInput): Promise<MessageRef> {
            return { channelId: await resolveLarkMessageRef(input.commonMessageId) };
        },

        async recordOutboundMessage(input: OutboundMessageRecordInput): Promise<string> {
            const botConfig = multiBotManager.getBotConfig(input.botName);
            const senderDisplayName =
                botConfig?.channel === 'lark'
                    ? (multiBotManager.getDisplayNameByAppId(
                          larkCredentials(botConfig).app_id,
                      ) ?? undefined)
                    : undefined;

            return storeLarkOutboundMessage({
                omId: input.channelMessageId,
                chatId: input.channelConversationId,
                commonConversationId: input.commonConversationId,
                commonRootMessageId: input.commonRootMessageId,
                commonReplyMessageId: input.commonReplyMessageId,
                contentText: input.contentText,
                botName: input.botName,
                senderDisplayName,
                scope: input.scope,
                eventTime: input.eventTime,
                messageType: input.messageType,
                responseId: input.responseId,
            });
        },

        async sendText(
            conv: ConversationRef,
            content: ContentItem[],
            ctx: RenderContext,
        ): Promise<MessageRef> {
            const post = await renderToPost(deps, content, ctx);
            const resp = await deps.send(conv.channelId, post);
            return { channelId: resp?.message_id ?? '' };
        },

        async reply(
            thread: ThreadRef,
            content: ContentItem[],
            ctx: RenderContext,
        ): Promise<MessageRef> {
            const anchor = resolveReplyAnchor(thread);
            const post = await renderToPost(deps, content, ctx);
            const resp = await deps.reply(anchor, post, thread.inThread === true);
            return { channelId: resp?.message_id ?? '' };
        },

        async recall(msg: MessageRef): Promise<void> {
            await deps.deleteMessage(msg.channelId);
        },
    };
}
