// chat-response-worker 出站反查决策（平台无关策略）。
//
// 两种出站，反查方式不同：
//   (a) 被动回复：payload.message_id 是真实来源消息的 common id。走完整
//       resolveOutboundTarget（source message + conversation + root），最终回复那条消息。
//   (b) 主动发（is_proactive=true）：赤尾凭生活节奏主动找真人说话，没有来源消息。
//       agent-service 给的 message_id 是伪 id `proactive:<uuid5>`，拿它去反查 lark_message
//       必 miss、抛 cannot-resolve。这条路径必须【跳过来源消息反查】，只用 chat_id
//       （真实 common_conversation_id）解析出渠道裸会话 id，往这个会话新发一条。
//       channelMessageId / channelRootMessageId 没有来源、留空；dispatch 据
//       isProactive + 无 root → sendText 新发（见 chat-response-outbound.ts）。

import type { OutboundCapabilities } from '@core/ports/channel-plugin';

export interface ChatResponseResolveInput {
    isProactive: boolean;
    messageId: string;
    chatId: string;
    rootId: string | undefined;
}

export interface ChatResponseChannelRefs {
    channelMessageId: string;
    channelConversationId: string;
    channelRootMessageId: string | undefined;
}

export async function resolveChatResponseOutboundRefs(
    cap: OutboundCapabilities,
    input: ChatResponseResolveInput,
): Promise<ChatResponseChannelRefs> {
    if (input.isProactive) {
        // 主动发 = 无条件新发，不 reply（语义钉死）。
        //
        // 这里【刻意忽略 input.rootId】：channelRootMessageId 永远返回 undefined，
        // 让下游 dispatch（chat-response-outbound.ts）的 proactive 分支走 sendText 新发、
        // 绝不走 reply。这是有意为之，不是漏判：
        //   1) 主动发是赤尾凭生活节奏主动找真人开口，本就该是一条新消息、没有要回复的
        //      来源消息（message_id 是 proactive: 伪 id，反查必 miss）。
        //   2) 当前没有任何会带 root_id 的主动发来源——agent-service emit 主动发时
        //      root_id=null。所以「is_proactive=true 且 root_id 非空」这个组合在现实里
        //      不存在；万一未来出现，按本函数的语义它仍走新发（root 被忽略），这正是
        //      主动发该有的行为，不会因为 root_id 偶然带值就退化成 reply。
        //   3) 绝不调 resolveOutboundTarget（会拿伪 message_id 去反查、抛 cannot-resolve）。
        // 只解析会话；查不到会话由 resolveConversationRef fail-loud。
        const conversation = await cap.resolveConversationRef(input.chatId);
        return {
            channelMessageId: '',
            channelConversationId: conversation.channelId,
            channelRootMessageId: undefined,
        };
    }

    const refs = await cap.resolveOutboundTarget({
        commonMessageId: input.messageId,
        commonConversationId: input.chatId,
        commonRootMessageId: input.rootId || undefined,
    });
    return {
        channelMessageId: refs.message.channelId,
        channelConversationId: refs.conversation.channelId,
        channelRootMessageId: refs.rootMessage?.channelId,
    };
}
