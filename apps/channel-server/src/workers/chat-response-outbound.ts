// chat-response-worker 出站派发（平台无关策略）。把「按 part_index / proactive
// 决定回复触发消息 vs 回复 root vs 新发」这个出站策略，从 worker inline 的飞书
// sendPost/replyPost 分支，改成选择调能力端口 capabilities.reply / sendText。
//
// 这层是平台无关的：任何 channel 都有「回复某条 vs 新发」的选择，端口只提供两个
// 原子能力，本函数据 part/proactive 选用哪个。飞书富文本渲染（@N.png 上传、
// @用户名 mention、markdown→PostContent）由能力端口内部做，本函数不碰飞书结构。
//
// 入参里的渠道裸 id（channelMessageId/channelConversationId/channelRootMessageId）
// 由 worker 通过当前 channel 插件从全局 id 反查得到；imageRegistryId 是【全局】
// message_id（图片注册表的 key），绝不是渠道裸 id —— 二者刻意分开，避免「用裸 id 查注册表
// 必 miss、图片被吞」那类回归（见 image-registry-key.ts / 对应回归测试）。

import type { OutboundCapabilities, MessageRef, RenderContext } from '@core/ports/channel-plugin';
import type { ContentItem } from '@core/channels/contracts';

// ---------------------------------------------------------------------------
// 出站反查策略（平台无关）：哪些 common id 需要翻成渠道裸 id。
//
// proactive 且无 root 的消息没有 inbound 锚点 —— message_id 是上游自造的合成
// 全局 id（如 agent-service persona review diff 推送的
// `persona-review:{lane}:{persona}:v{n}`），渠道映射表里没有这行，message 维度
// 反查必炸；而它的出站动作（dispatchChatResponseOutbound 同条件分支）只会是
// sendText(会话)，根本用不到消息锚点。所以这条路径只反查 conversation 维度
// （解析失败照样 fail-loud，绝不发进未知会话），channelMessageId 恒为 ''（仅
// reply 路径用得到，而该路径此时不可达；cap.reply 的空锚点 fail-loud 兜底）。
// 其余路径（非 proactive / proactive 有 root）维持全量反查，行为零变化。
// ---------------------------------------------------------------------------

export interface ChatResponseTargetInput {
    messageId: string; // common_message_id（proactive 无 root 时可能是合成 id，不入反查）
    conversationId: string; // common_conversation_id
    rootMessageId: string | undefined; // common root message id（proactive 的回复锚点）
    isProactive: boolean;
}

export interface ChatResponseResolvedTarget {
    channelMessageId: string;
    channelConversationId: string;
    channelRootMessageId: string | undefined;
}

export async function resolveChatResponseTarget(
    cap: OutboundCapabilities,
    input: ChatResponseTargetInput,
): Promise<ChatResponseResolvedTarget> {
    if (input.isProactive && !input.rootMessageId) {
        if (!cap.resolveConversationRef) {
            throw new Error(
                'channel capabilities missing resolveConversationRef; cannot resolve ' +
                    'the conversation for a proactive no-root outbound message',
            );
        }
        const conv = await cap.resolveConversationRef({
            commonConversationId: input.conversationId,
        });
        return {
            channelMessageId: '',
            channelConversationId: conv.channelId,
            channelRootMessageId: undefined,
        };
    }
    const refs = await cap.resolveOutboundTarget({
        commonMessageId: input.messageId,
        commonConversationId: input.conversationId,
        commonRootMessageId: input.rootMessageId,
    });
    return {
        channelMessageId: refs.message.channelId,
        channelConversationId: refs.conversation.channelId,
        channelRootMessageId: refs.rootMessage?.channelId,
    };
}

export interface ChatResponseOutboundInput {
    content: string; // AI 原始 markdown 文本（平台化由能力端口内部做）
    channelMessageId: string; // 触发消息渠道裸 id
    channelConversationId: string; // 会话渠道裸 id
    channelRootMessageId: string | undefined; // proactive 的 root 渠道裸 id
    imageRegistryId: string; // 全局 message_id（图片注册表 key），非渠道裸 id
    isP2p: boolean;
    partIndex: number;
    isProactive: boolean;
}

export async function dispatchChatResponseOutbound(
    cap: OutboundCapabilities,
    input: ChatResponseOutboundInput,
): Promise<MessageRef> {
    const content: ContentItem[] = [{ kind: 'text', text: input.content }];
    const ctx: RenderContext = {
        imageRegistryId: input.imageRegistryId,
        // worker 侧入参是渠道裸会话 id；映射到端口契约的中性字段 groupConversationId。
        groupConversationId: input.channelConversationId,
        // 群聊解析 @用户名 mention；私聊跳过（与现状 is_p2p ? content : resolve 一致）。
        resolveMentions: !input.isP2p,
    };

    if (input.partIndex === 0) {
        if (input.isProactive) {
            // proactive：有 root 回复 root，无 root 新发到会话。
            if (input.channelRootMessageId) {
                return cap.reply(
                    { selfChannelMessageId: input.channelRootMessageId },
                    content,
                    ctx,
                );
            }
            return cap.sendText({ channelId: input.channelConversationId }, content, ctx);
        }
        // 非 proactive：回复触发消息本身。不要默认开启飞书 thread；是否进 thread
        // 必须由调用方显式给 inThread=true，避免普通聊天回复被挂进话题串。
        return cap.reply(
            { selfChannelMessageId: input.channelMessageId },
            content,
            ctx,
        );
    }

    // part > 0：续段新发到会话。
    return cap.sendText({ channelId: input.channelConversationId }, content, ctx);
}
