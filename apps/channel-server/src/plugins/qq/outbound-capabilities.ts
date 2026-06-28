// QQ 出站能力端口实现（OutboundCapabilities）。整个仓库 QQ 出站发回网关的唯一出口。
//
// 控制方向：worker（core 边缘）只产出平台无关 ContentItem[] + RenderContext，本模块
// 把它表达成 CustomOutboundMessage（@inner/shared/protocols）经 LaneRouter POST 给
// qq-gateway 的 POST /qq/outbound。被动窗口、msg_seq、4 次上限由网关独占；channel-server
// 这侧只负责把「所回应的原始 QQ msg_id」一路带到出站。
//
// 关键约束（QQ 官方机器人发不出主动消息）：
//   - 被动回复（reply / part0）：thread 锚点已是 resolveOutboundTarget 反查出的原始
//     QQ msg_id → 直接作 replyToMessageId。
//   - 多段续段（sendText, part>0）：reply/sendText 端口签名不带 partIndex，但 worker
//     把【源 common message id】放在 ctx.imageRegistryId（见 image-registry-key.ts）。
//     用它反查 qq_message 拿回原始 QQ msg_id，让续段仍挂在同一 msg_id 下。
//   - 主动发（is_proactive / 反查不到原始 msg_id，如 proactive:<uuid> 伪 id）：抛错
//     fail-loud，不发网关（发了也会被 QQ 拒）。网关回执 sent=false（超窗 / 超 4 次 /
//     发送报错）同样抛错。两者都由通用 handler 的 catch 兜住（记 error、不 record），
//     绝不用合成 id 兜底落库，避免把没发出的消息污染进 qq_message。
//
// idempotencyKey 基于「源 common message id + 段序 partIndex」派生（MQ 重投同一段
// (message_id, part_index) 稳定 → 同 key → 网关去重；同会话相同文本的不同续段不会撞）。

import type {
    CommonMessageResolveInput,
    ConversationRef,
    MessageRef,
    OutboundCapabilities,
    OutboundMessageRecordInput,
    OutboundTargetResolveInput,
    RenderContext,
} from '@core/ports/channel-plugin';
import type { ContentItem, ThreadRef } from '@core/channels/contracts';
import type { CustomChatType, CustomOutboundMessage, CustomOutboundResult } from '@inner/shared/protocols';
import { validateCustomOutboundMessage } from '@inner/shared/protocols';
import { context } from '@middleware/context';
import { storeQqOutboundMessage } from './common-projector';
import {
    resolveQqConversationRef,
    resolveQqMessageRef,
    reverseResolveOutbound,
} from './outbound-reverse-resolve';

// 出站发回网关的 I/O 协作者。注入 → 渲染/装配逻辑可测，网关耦合只在 default。
export interface QqOutboundDeps {
    // POST CustomOutboundMessage 到 qq-gateway，返回网关回执。回执 sent 是唯一权威：
    // 网关裁决超窗 / 超 4 次 / 主动发 / 发送报错，sent=false 时本侧必须 fail-loud，
    // 不得用合成 id 兜底落库（否则把没发出的消息污染进 qq_message）。
    postOutbound(msg: CustomOutboundMessage): Promise<CustomOutboundResult>;
}

function extractText(content: ContentItem[]): string {
    return content
        .map((c) => (c.kind === 'text' || c.kind === 'unsupported' ? c.text : ''))
        .join('');
}

function resolveReplyAnchor(thread: ThreadRef): string {
    const anchor =
        thread.selfChannelMessageId ??
        thread.replyToChannelMessageId ??
        thread.rootChannelMessageId;
    if (!anchor) {
        throw new Error(
            'qq reply called with a ThreadRef that has no usable anchor ' +
                '(self/replyTo/root all empty); refusing to send to an empty target',
        );
    }
    return anchor;
}

function chatTypeFromCtx(ctx: RenderContext): CustomChatType {
    // worker 出站 ctx.resolveMentions = !is_p2p：false=私聊、true/undefined=群聊。
    return ctx.resolveMentions === false ? 'direct' : 'group';
}

function requireConversationId(ctx: RenderContext): string {
    const conversationId = ctx.groupConversationId;
    if (!conversationId) {
        throw new Error(
            'qq outbound missing conversation id (ctx.groupConversationId); ' +
                'cannot address a CustomOutboundMessage',
        );
    }
    return conversationId;
}

function buildOutbound(args: {
    replyToMessageId: string;
    conversationId: string;
    chatType: CustomChatType;
    text: string;
    idempotencySource: string;
    partIndex: number;
}): CustomOutboundMessage {
    const botName = context.getBotName() || '';
    return validateCustomOutboundMessage({
        botName,
        chatType: args.chatType,
        conversationId: args.conversationId,
        replyToMessageId: args.replyToMessageId,
        text: args.text,
        // 幂等键 = qq:<源 common message id>:<段序>。MQ 重投同一 (源消息, 段序) → 同
        // key → 网关去重；相同文本的不同续段段序不同 → key 不同，不会被误判 duplicate。
        idempotencyKey: `qq:${args.idempotencySource}:${args.partIndex}`,
    });
}

// POST 给网关并以回执 sent 为准：sent=false（超窗 / 超 4 次 / 主动发 / 发送报错）
// 抛描述性错误，由通用 chat-response handler 的 catch 兜住（记 error、不 record），
// 不返回空 channelId、不让没发出的消息污染 qq_message。
async function postOrThrow(deps: QqOutboundDeps, out: CustomOutboundMessage): Promise<MessageRef> {
    const resp = await deps.postOutbound(out);
    // KNOWN RESIDUAL（标注不修，留后续）：网关对 duplicate 回执（MQ 重投同一段、或网关崩溃后
    // 重投）会返回 sent=false（reason=duplicate）。此处一律按失败抛出，可能把一条「其实已
    // 经送达」的 response 误标为 failed。罕见，且消息已实际送达，本期不改逻辑——后续如要精确，
    // 需让网关把 duplicate 与真失败区分（如回 sent=true + 标记 duplicate，或单独的 outcome 字段）。
    if (!resp.sent) {
        throw new Error(
            `qq outbound not sent: gateway dropped/failed (reason=${resp.reason ?? 'unknown'}, ` +
                `replyTo=${out.replyToMessageId ?? '<none>'}, idem=${out.idempotencyKey}); ` +
                'refusing to record an unsent message',
        );
    }
    return { channelId: resp.messageId ?? '' };
}

export function createQqOutboundCapabilities(deps: QqOutboundDeps): OutboundCapabilities {
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
                rootMessage: refs.channelRootId ? { channelId: refs.channelRootId } : undefined,
            };
        },

        async resolveMessageRef(input: CommonMessageResolveInput): Promise<MessageRef> {
            return { channelId: await resolveQqMessageRef(input.commonMessageId) };
        },

        async resolveConversationRef(commonConversationId: string): Promise<ConversationRef> {
            return resolveQqConversationRef(commonConversationId);
        },

        async recordOutboundMessage(input: OutboundMessageRecordInput): Promise<string> {
            return storeQqOutboundMessage({
                qqMessageId: input.channelMessageId,
                conversationId: input.channelConversationId,
                commonConversationId: input.commonConversationId,
                commonRootMessageId: input.commonRootMessageId,
                commonReplyMessageId: input.commonReplyMessageId,
                contentText: input.contentText,
                botName: input.botName,
                scope: input.scope,
                eventTime: input.eventTime,
                messageType: input.messageType,
                responseId: input.responseId,
            });
        },

        // 续段 / 主动发都走这里。续段：用 ctx.imageRegistryId（源 common message id）
        // 反查原始 QQ msg_id；反查不到 = 主动发 → 丢弃 + warn、不发网关。
        async sendText(
            _conv: ConversationRef,
            content: ContentItem[],
            ctx: RenderContext,
        ): Promise<MessageRef> {
            const sourceCommonId = ctx.imageRegistryId;
            let replyToMessageId: string | undefined;
            if (sourceCommonId) {
                try {
                    replyToMessageId = await resolveQqMessageRef(sourceCommonId);
                } catch {
                    replyToMessageId = undefined;
                }
            }
            if (!replyToMessageId) {
                // 反查不到原始 QQ msg_id = 主动发 / 超窗，QQ 官方机器人发不出来。
                // fail-loud（不返回空 channelId）：handler catch 兜住、不 record，
                // 否则会用合成 id 把没发出的消息污染进 qq_message。
                throw new Error(
                    `qq outbound: no resolvable inbound msg_id ` +
                        `(proactive / out-of-window not deliverable on QQ): ` +
                        `source=${sourceCommonId ?? 'none'}`,
                );
            }
            const out = buildOutbound({
                replyToMessageId,
                conversationId: requireConversationId(ctx),
                chatType: chatTypeFromCtx(ctx),
                text: extractText(content),
                idempotencySource: sourceCommonId ?? replyToMessageId,
                partIndex: ctx.partIndex ?? 0,
            });
            return postOrThrow(deps, out);
        },

        // 被动回复（part0）。thread 锚点已是反查出的原始 QQ msg_id。
        async reply(
            thread: ThreadRef,
            content: ContentItem[],
            ctx: RenderContext,
        ): Promise<MessageRef> {
            const replyToMessageId = resolveReplyAnchor(thread);
            const out = buildOutbound({
                replyToMessageId,
                conversationId: requireConversationId(ctx),
                chatType: chatTypeFromCtx(ctx),
                text: extractText(content),
                idempotencySource: ctx.imageRegistryId ?? replyToMessageId,
                partIndex: ctx.partIndex ?? 0,
            });
            return postOrThrow(deps, out);
        },

        // recall：QQ 暂不实现（能力可选，依赖它的指令对 QQ 自然不可用）。
    };
}
