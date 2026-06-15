import { describe, it, expect } from 'bun:test';

import { resolveChatResponseOutboundRefs } from './chat-response-resolve';
import type {
    OutboundCapabilities,
    OutboundResolvedTarget,
    OutboundTargetResolveInput,
    ConversationRef,
} from '@core/ports/channel-plugin';

// chat-response-worker 出站反查决策（平台无关策略）。
//
// 两种出站：
//   (a) 被动回复：payload.message_id 是真实来源消息的 common id，要走完整反查
//       （source message + conversation + root），最终回复那条消息。
//   (b) 主动发（is_proactive=true）：赤尾凭生活节奏主动找真人说话，没有来源消息。
//       agent-service 给的 message_id 是伪 id `proactive:<uuid5>`，反查必 miss。
//       这条路径必须【跳过来源消息反查】，只把 chat_id（真实 common_conversation_id）
//       解析成渠道裸会话 id，往这个会话新发一条消息。
//
// 本测试钉死：proactive 分支绝不调 resolveOutboundTarget（那会拿伪 message_id 去
// 反查、抛 cannot-resolve），只调 resolveConversationRef；非 proactive 分支走完整
// resolveOutboundTarget。

// ---- 可注入的 cap spy（不碰真实 DB / lark 映射表）----
function makeCap(over: {
    resolveOutboundTarget?: (input: OutboundTargetResolveInput) => Promise<OutboundResolvedTarget>;
    resolveConversationRef?: (commonConversationId: string) => Promise<ConversationRef>;
}): {
    cap: OutboundCapabilities;
    calls: {
        resolveOutboundTarget: OutboundTargetResolveInput[];
        resolveConversationRef: string[];
    };
} {
    const calls = {
        resolveOutboundTarget: [] as OutboundTargetResolveInput[],
        resolveConversationRef: [] as string[],
    };
    const cap = {
        async resolveOutboundTarget(input: OutboundTargetResolveInput) {
            calls.resolveOutboundTarget.push(input);
            if (over.resolveOutboundTarget) return over.resolveOutboundTarget(input);
            throw new Error(`lark outbound cannot resolve common_message_id=${input.commonMessageId}`);
        },
        async resolveConversationRef(commonConversationId: string) {
            calls.resolveConversationRef.push(commonConversationId);
            if (over.resolveConversationRef) return over.resolveConversationRef(commonConversationId);
            return { channelId: `oc_for_${commonConversationId}` };
        },
        // 端口其余方法本测试不用，给空实现满足类型。
        async resolveMessageRef() {
            return { channelId: '' };
        },
        async recordOutboundMessage() {
            return '';
        },
        async sendText() {
            return { channelId: '' };
        },
        async reply() {
            return { channelId: '' };
        },
    } as unknown as OutboundCapabilities;
    return { cap, calls };
}

describe('resolveChatResponseOutboundRefs', () => {
    it('被动回复：走完整 resolveOutboundTarget，返回 message/conversation/root 三个裸 id', async () => {
        const { cap, calls } = makeCap({
            resolveOutboundTarget: async () => ({
                message: { channelId: 'om_msg' },
                conversation: { channelId: 'oc_chat' },
                rootMessage: { channelId: 'om_root' },
            }),
        });

        const refs = await resolveChatResponseOutboundRefs(cap, {
            isProactive: false,
            messageId: '018f-common-msg',
            chatId: '018f-common-chat',
            rootId: '018f-common-root',
        });

        expect(refs.channelMessageId).toBe('om_msg');
        expect(refs.channelConversationId).toBe('oc_chat');
        expect(refs.channelRootMessageId).toBe('om_root');
        // 完整反查被调一次，不碰会话单独反查
        expect(calls.resolveOutboundTarget.length).toBe(1);
        expect(calls.resolveConversationRef.length).toBe(0);
    });

    it('主动发：跳过来源消息反查，只解析会话；channelMessageId/root 为空，不抛 cannot-resolve', async () => {
        const { cap, calls } = makeCap({
            resolveConversationRef: async (id) => {
                expect(id).toBe('018f-real-p2p-chat');
                return { channelId: 'oc_real_p2p' };
            },
        });

        const refs = await resolveChatResponseOutboundRefs(cap, {
            isProactive: true,
            // 主动发的伪 id：拿它去反查必 miss，这条路径必须绕开
            messageId: 'proactive:550e8400-e29b-41d4-a716-446655440000',
            chatId: '018f-real-p2p-chat',
            rootId: undefined,
        });

        // 用 chat_id 反查出的飞书裸会话 id 走新发
        expect(refs.channelConversationId).toBe('oc_real_p2p');
        // 没有来源消息：message/root 裸 id 留空
        expect(refs.channelMessageId).toBe('');
        expect(refs.channelRootMessageId).toBeUndefined();
        // 关键：绝不调完整反查（否则会拿 proactive: 伪 id 去查、抛 cannot-resolve）
        expect(calls.resolveOutboundTarget.length).toBe(0);
        expect(calls.resolveConversationRef).toEqual(['018f-real-p2p-chat']);
    });

    it('主动发：会话反查失败要 fail-loud（resolver 说发不了就发不了，不静默）', async () => {
        const { cap } = makeCap({
            resolveConversationRef: async (id) => {
                throw new Error(`lark outbound cannot resolve common_conversation_id=${id}`);
            },
        });

        await expect(
            resolveChatResponseOutboundRefs(cap, {
                isProactive: true,
                messageId: 'proactive:abc',
                chatId: '018f-missing-chat',
                rootId: undefined,
            }),
        ).rejects.toThrow(/common_conversation_id=018f-missing-chat/);
    });
});
