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
        // 段序透传：出站幂等键据它区分相同文本的不同续段（lark 不读、安全）。
        partIndex: input.partIndex,
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
