import { describe, it, expect } from 'bun:test';

import { handleChatResponse } from './chat-response-handler';
import type { ChatResponseHandlerDeps } from './chat-response-handler';
import type {
    OutboundCapabilities,
    OutboundTargetResolveInput,
    CommonMessageResolveInput,
    OutboundMessageRecordInput,
    ConversationRef,
    MessageRef,
    RenderContext,
} from '@core/ports/channel-plugin';
import type { ContentItem, ThreadRef } from '@core/channels/contracts';
import type { ConsumeMessage } from 'amqplib';

// 主动发（is_proactive）worker 端到端测试。
//
// 背景：赤尾凭生活节奏主动给真人发消息时，agent-service 往 chat_response 队列
// emit 一条 payload：
//   is_proactive=true、message_id='proactive:<uuid5>'（不是真实来源消息 id）、
//   chat_id=真实 common_conversation_id、bot_name 来自 payload、
//   session_id=null、root_id=null。
//
// 这个测试喂接近真实 MQ payload 的主动发消息，跑整条 handleChatResponse 链，钉死：
//   (1) 整条链不炸（不抛、ack 一次、不 nack）；
//   (2) 走「新发」（sendText），不是 reply；
//   (3) 绝不对 'proactive:' 伪 id 做来源消息反查（resolveOutboundTarget /
//       resolveMessageRef 零调用）；
//   (4) recordOutboundMessage 在 session_id=null / responseId=null 下正常调用，
//       提交的中性字段口径正确（root/reply 都不挂 proactive 伪 id）；
//   (5) session_id 为空时绝不查 agent_response、绝不写 replies / status。

// ---- 可注入的 cap spy（不碰真实 DB / lark 映射表）----
function makeCap(): {
    cap: OutboundCapabilities;
    calls: {
        resolveOutboundTarget: OutboundTargetResolveInput[];
        resolveMessageRef: CommonMessageResolveInput[];
        resolveConversationRef: string[];
        recordOutboundMessage: OutboundMessageRecordInput[];
        sendText: Array<{ conv: ConversationRef; content: ContentItem[]; ctx: RenderContext }>;
        reply: Array<{ thread: ThreadRef; content: ContentItem[]; ctx: RenderContext }>;
    };
} {
    const calls = {
        resolveOutboundTarget: [] as OutboundTargetResolveInput[],
        resolveMessageRef: [] as CommonMessageResolveInput[],
        resolveConversationRef: [] as string[],
        recordOutboundMessage: [] as OutboundMessageRecordInput[],
        sendText: [] as Array<{ conv: ConversationRef; content: ContentItem[]; ctx: RenderContext }>,
        reply: [] as Array<{ thread: ThreadRef; content: ContentItem[]; ctx: RenderContext }>,
    };
    const cap: OutboundCapabilities = {
        async resolveOutboundTarget(input) {
            calls.resolveOutboundTarget.push(input);
            // 主动发拿伪 message_id 来这里反查 = bug；模拟真实 lark fail-loud。
            throw new Error(
                `lark outbound cannot resolve common_message_id=${input.commonMessageId}`,
            );
        },
        async resolveMessageRef(input) {
            calls.resolveMessageRef.push(input);
            throw new Error(
                `lark outbound cannot resolve common_message_id=${input.commonMessageId}`,
            );
        },
        async resolveConversationRef(commonConversationId) {
            calls.resolveConversationRef.push(commonConversationId);
            return { channelId: `oc_for_${commonConversationId}` };
        },
        async recordOutboundMessage(input) {
            calls.recordOutboundMessage.push(input);
            return 'common_assistant_msg_id';
        },
        async sendText(conv, content, ctx): Promise<MessageRef> {
            calls.sendText.push({ conv, content, ctx });
            return { channelId: 'om_new_proactive_msg' };
        },
        async reply(thread, content, ctx): Promise<MessageRef> {
            calls.reply.push({ thread, content, ctx });
            return { channelId: 'om_reply_msg' };
        },
    };
    return { cap, calls };
}

// agent_response repo spy。主动发不该碰它，所以任何 findOneBy / update 都记下来断言为空。
function makeRepoSpy(): {
    repo: ChatResponseHandlerDeps['repo'];
    calls: { findOneBy: unknown[]; update: unknown[]; createQueryBuilder: number };
} {
    const calls = { findOneBy: [] as unknown[], update: [] as unknown[], createQueryBuilder: 0 };
    const repo = {
        findOneBy: async (where: unknown) => {
            calls.findOneBy.push(where);
            return null;
        },
        update: async (where: unknown, _patch: unknown) => {
            calls.update.push(where);
            return { affected: 0 };
        },
        createQueryBuilder: () => {
            calls.createQueryBuilder++;
            // 主动发不该走到这里；给一个会炸的链路，万一调用就让测试 fail。
            throw new Error('createQueryBuilder must not be called on proactive path');
        },
    } as unknown as ChatResponseHandlerDeps['repo'];
    return { repo, calls };
}

function makeMsg(payload: Record<string, unknown>): ConsumeMessage {
    return {
        content: Buffer.from(JSON.stringify(payload)),
        fields: {} as ConsumeMessage['fields'],
        properties: { headers: {} } as ConsumeMessage['properties'],
    } as ConsumeMessage;
}

// 接近真实 MQ 的主动发 payload（agent-service emit 口径）。
function proactivePayload() {
    return {
        channel: 'lark',
        is_proactive: true,
        message_id: 'proactive:550e8400-e29b-41d4-a716-446655440000',
        chat_id: '018f-real-p2p-conversation',
        bot_name: 'akao',
        session_id: null,
        root_id: null,
        is_p2p: true,
        user_id: 'ou_real_user',
        content: '在吗？我刚刚在想你',
        full_content: '在吗？我刚刚在想你',
        status: 'success',
        part_index: 0,
        is_last: true,
        persona_id: 'persona_akao',
    };
}

function makeDeps(over: {
    cap?: OutboundCapabilities;
    repo?: ChatResponseHandlerDeps['repo'];
    ack?: () => void;
    nack?: () => void;
}): ChatResponseHandlerDeps {
    return {
        repo: over.repo ?? (makeRepoSpy().repo),
        getCapabilities: (_channel: string) => over.cap ?? makeCap().cap,
        ack: over.ack ?? (() => {}),
        nack: over.nack ?? (() => {}),
        observeDuration: () => {},
        observeQueueDelay: () => {},
    };
}

describe('handleChatResponse — 主动发端到端', () => {
    it('真实主动发 payload：整链不炸、ack 一次、不 nack', async () => {
        const { cap } = makeCap();
        let acks = 0;
        let nacks = 0;
        const deps = makeDeps({ cap, ack: () => acks++, nack: () => nacks++ });

        await handleChatResponse(deps, makeMsg(proactivePayload()));

        expect(acks).toBe(1);
        expect(nacks).toBe(0);
    });

    it('走「新发」sendText（不是 reply），发到 chat_id 反查出的会话', async () => {
        const { cap, calls } = makeCap();
        const deps = makeDeps({ cap });

        await handleChatResponse(deps, makeMsg(proactivePayload()));

        expect(calls.reply.length).toBe(0);
        expect(calls.sendText.length).toBe(1);
        expect(calls.sendText[0].conv.channelId).toBe('oc_for_018f-real-p2p-conversation');
        expect(calls.sendText[0].content).toEqual([{ kind: 'text', text: '在吗？我刚刚在想你' }]);
        // p2p：不解析 mention
        expect(calls.sendText[0].ctx.resolveMentions).toBe(false);
        // 会话反查恰好用真实 chat_id
        expect(calls.resolveConversationRef).toEqual(['018f-real-p2p-conversation']);
    });

    it('绝不对 proactive: 伪 id 做来源消息反查', async () => {
        const { cap, calls } = makeCap();
        const deps = makeDeps({ cap });

        await handleChatResponse(deps, makeMsg(proactivePayload()));

        expect(calls.resolveOutboundTarget.length).toBe(0);
        expect(calls.resolveMessageRef.length).toBe(0);
    });

    it('recordOutboundMessage 在 session_id=null / responseId=null 下正常调用，root/reply 不挂 proactive 伪 id', async () => {
        const { cap, calls } = makeCap();
        const deps = makeDeps({ cap });

        await handleChatResponse(deps, makeMsg(proactivePayload()));

        expect(calls.recordOutboundMessage.length).toBe(1);
        const rec = calls.recordOutboundMessage[0];
        expect(rec.commonConversationId).toBe('018f-real-p2p-conversation');
        expect(rec.botName).toBe('akao');
        expect(rec.scope).toBe('direct');
        // session_id=null → responseId 不挂值
        expect(rec.responseId ?? null).toBeNull();
        // root_id=null 的主动发：绝不把 proactive: 伪 id 写进 root/reply 映射
        expect(rec.commonRootMessageId ?? null).toBeNull();
        expect(rec.commonReplyMessageId ?? null).toBeNull();
        expect(rec.commonRootMessageId).not.toBe('proactive:550e8400-e29b-41d4-a716-446655440000');
        expect(rec.commonReplyMessageId).not.toBe('proactive:550e8400-e29b-41d4-a716-446655440000');
        // 新发后渠道裸 id 作为 channelMessageId 落库
        expect(rec.channelMessageId).toBe('om_new_proactive_msg');
    });

    it('session_id 为空：绝不查 agent_response、绝不写 replies / status', async () => {
        const { cap } = makeCap();
        const { repo, calls: repoCalls } = makeRepoSpy();
        const deps = makeDeps({ cap, repo });

        await handleChatResponse(deps, makeMsg(proactivePayload()));

        expect(repoCalls.findOneBy.length).toBe(0);
        expect(repoCalls.update.length).toBe(0);
        expect(repoCalls.createQueryBuilder).toBe(0);
    });
});

describe('handleChatResponse — 出站失败显眼日志', () => {
    it('发飞书失败：记 error 级显眼日志（带 chat_id / bot_name / persona_id），仍 ack 不 nack', async () => {
        const { cap } = makeCap();
        // 让 sendText 抛错模拟发飞书失败
        cap.sendText = async () => {
            throw new Error('lark send failed: rate limited');
        };

        const errorLogs: unknown[][] = [];
        const origError = console.error;
        console.error = (...args: unknown[]) => {
            errorLogs.push(args);
        };

        let acks = 0;
        let nacks = 0;
        try {
            const deps = makeDeps({ cap, ack: () => acks++, nack: () => nacks++ });
            await handleChatResponse(deps, makeMsg(proactivePayload()));
        } finally {
            console.error = origError;
        }

        // 仍 ack（异步失败回流是下一刀；这一刀只要求别静默吞 + 显眼日志）
        expect(acks).toBe(1);
        expect(nacks).toBe(0);

        // 出站失败必须有一条 error 日志，且携带够排查的字段
        const blob = errorLogs.map((a) => a.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join(' ')).join('\n');
        expect(blob).toContain('018f-real-p2p-conversation'); // chat_id
        expect(blob).toContain('akao'); // bot_name
        expect(blob).toContain('persona_akao'); // persona_id
    });
});
