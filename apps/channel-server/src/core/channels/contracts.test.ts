import { describe, it, expect } from 'bun:test';

import {
    type InboundMessage,
    type InboundAdapter,
    type OutboundAdapter,
    type AddressingPolicy,
    type AddressingDecision,
    type ReplyTarget,
    assertValidInboundMessage,
    deliver,
    enforceDecision,
} from './contracts';

// 这组测试用一个"假想的纯 HTTP 问答 channel"当验证载体：它跟 IM 形态差别很大
// （没有 webhook 握手、没有 @、没有群、没有回复树）。spec 的验收底线是：接这种
// channel 只需实现三件套（InboundAdapter / OutboundAdapter / AddressingPolicy），
// 不碰核心、不碰别的 adapter。如果这个最小实现写不出来，说明契约被 IM 绑架了。

// ---- 假想 HTTP 问答 channel 的三件套实现（全部在测试内，是契约的可执行规格）----

const httpInbound: InboundAdapter = {
    handleHandshake() {
        return null; // 没有握手
    },
    verify() {
        return true; // 它自己的鉴权方式，这里简化
    },
    parse(raw: { qid: string; user: string; question: string }): InboundMessage {
        return {
            channel: 'http-qa',
            bot_name: 'qa-bot',
            channel_message_id: raw.qid,
            channel_chat_id: raw.user, // 一问一答，会话就是这个用户
            channel_user_id: raw.user,
            conversation_scope: 'direct',
            thread_ref: null,
            addressing_hints: [],
            content: [{ kind: 'text', text: raw.question }],
            received_at: 0,
        };
    },
};

// adapter 只实现两个原子操作；"无回复语义时退化为 send 该发到哪" 不再由
// 每个 adapter 自己写（旧实现硬编码 'degraded' 掩盖了它根本不知道发哪），
// 而是由 contracts.ts 的中心化 deliver() 决定。
const sent: Array<{ via: 'send' | 'reply'; target: string; text: string }> = [];
const httpOutbound: OutboundAdapter = {
    send(channelChatId, content) {
        sent.push({ via: 'send', target: channelChatId, text: content });
        return Promise.resolve('out-' + channelChatId);
    },
    reply(threadRef, content) {
        sent.push({ via: 'reply', target: JSON.stringify(threadRef), text: content });
        return Promise.resolve('out-reply');
    },
};

const httpPolicy: AddressingPolicy = {
    decide(): AddressingDecision {
        return { respond: true, reason: 'http-qa always answers' };
    },
};

describe('channel 四层契约 — 用假想 HTTP 问答 channel 验证不被 IM 绑架', () => {
    it('InboundAdapter.parse 产出合法的、无 IM 假设的 InboundMessage', () => {
        const msg = httpInbound.parse({ qid: 'q1', user: 'u1', question: 'hello?' });
        expect(msg.channel).toBe('http-qa');
        expect(msg.conversation_scope).toBe('direct');
        expect(msg.thread_ref).toBeNull(); // 无回复树
        expect(msg.addressing_hints).toEqual([]); // 无 @
        expect(msg.content).toEqual([{ kind: 'text', text: 'hello?' }]);
        // 运行时契约守卫：合法消息不抛
        expect(() => assertValidInboundMessage(msg)).not.toThrow();
    });

    it('assertValidInboundMessage 挡住缺必填字段的非法消息', () => {
        const bad = { ...httpInbound.parse({ qid: 'q', user: 'u', question: 'x' }) } as Record<
            string,
            unknown
        >;
        delete bad.channel;
        expect(() => assertValidInboundMessage(bad)).toThrow();
    });

    it('不需要握手的 channel：handleHandshake 返回 null 是合法的', () => {
        expect(httpInbound.handleHandshake({})).toBeNull();
    });

    it('deliver 在无回复语义(threadRef=null)时退化为 send 到 channelChatId', async () => {
        sent.length = 0;
        const target: ReplyTarget = { channelChatId: 'u9', threadRef: null };
        await deliver(httpOutbound, target, 'the answer');
        expect(sent).toHaveLength(1);
        expect(sent[0].via).toBe('send');
        // 关键：必须发到真实会话 u9，而不是旧实现硬编码的 'degraded'
        expect(sent[0].target).toBe('u9');
    });

    it('deliver 在有 threadRef 时走 reply 语义', async () => {
        sent.length = 0;
        const target: ReplyTarget = {
            channelChatId: 'u9',
            threadRef: { replyToChannelMessageId: 'm1' },
        };
        await deliver(httpOutbound, target, 'the answer');
        expect(sent).toHaveLength(1);
        expect(sent[0].via).toBe('reply');
    });

    it('AddressingPolicy 返回带 reason 的决策，不是裸 bool', () => {
        const d = httpPolicy.decide({} as InboundMessage, 'qa-bot');
        expect(d.respond).toBe(true);
        expect(typeof d.reason).toBe('string');
        expect(d.reason.length).toBeGreaterThan(0);
    });
});

// ---- 一个 IM 风格的 AddressingPolicy：验证 direct/group 行为且"不响应必有理由" ----

const imPolicy: AddressingPolicy = {
    decide(msg: InboundMessage, botIdentity: string): AddressingDecision {
        if (msg.conversation_scope === 'direct') {
            return { respond: true, reason: 'direct message' };
        }
        const hit = msg.addressing_hints.some((h) => h.targetId === botIdentity);
        return hit
            ? { respond: true, reason: 'bot addressed in group' }
            : { respond: false, reason: 'group message not addressed to this bot' };
    },
};

const baseMsg: InboundMessage = {
    channel: 'lark',
    bot_name: 'b',
    channel_message_id: 'm',
    channel_chat_id: 'c',
    channel_user_id: 'u',
    conversation_scope: 'direct',
    thread_ref: null,
    addressing_hints: [],
    content: [{ kind: 'text', text: 'hi' }],
    received_at: 0,
};

describe('AddressingPolicy — direct/group 行为与“不静默”契约', () => {
    it('direct 直通', () => {
        const d = imPolicy.decide({ ...baseMsg, conversation_scope: 'direct' }, 'BOT');
        expect(d.respond).toBe(true);
    });

    it('group 且未命中 bot：不响应，但 reason 必须非空（杜绝静默丢弃）', () => {
        const d = imPolicy.decide(
            { ...baseMsg, conversation_scope: 'group', addressing_hints: [] },
            'BOT',
        );
        expect(d.respond).toBe(false);
        expect(d.reason.trim().length).toBeGreaterThan(0);
    });

    it('group 且命中 bot：响应', () => {
        const d = imPolicy.decide(
            {
                ...baseMsg,
                conversation_scope: 'group',
                addressing_hints: [{ targetId: 'BOT' }],
            },
            'BOT',
        );
        expect(d.respond).toBe(true);
    });
});

describe('enforceDecision — 把“不响应必带可记录 reason”从约定变成强制', () => {
    it('respond=true：返回 true，不记日志', () => {
        const logs: string[] = [];
        const go = enforceDecision({ respond: true, reason: 'direct' }, (r) =>
            logs.push(r),
        );
        expect(go).toBe(true);
        expect(logs).toEqual([]);
    });

    it('respond=false 且 reason 非空：返回 false，并把 reason 交给日志', () => {
        const logs: string[] = [];
        const go = enforceDecision(
            { respond: false, reason: 'group message not addressed to this bot' },
            (r) => logs.push(r),
        );
        expect(go).toBe(false);
        expect(logs).toEqual(['group message not addressed to this bot']);
    });

    it('respond=false 但 reason 为空：直接抛错（连理由都没有就是静默丢弃 bug）', () => {
        expect(() =>
            enforceDecision({ respond: false, reason: '   ' }, () => {}),
        ).toThrow();
    });
});
