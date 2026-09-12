import { describe, it, expect } from 'bun:test';

import {
    type ContentItem,
    type InboundMessage,
    type InboundAdapter,
    type AddressingPolicy,
    type AddressingDecision,
    assertValidInboundMessage,
    enforceDecision,
    summarizeContent,
} from './contracts';

// 这组测试用一个"假想的纯 HTTP 问答 channel"当验证载体：它跟 IM 形态差别很大
// （没有 webhook 握手、没有 @、没有群、没有回复树）。spec 的验收底线是：接这种
// channel 只需实现入站 + 寻址（InboundAdapter / AddressingPolicy），不碰核心、
// 不碰别的 adapter。如果这个最小实现写不出来，说明契约被 IM 绑架了。

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

const httpPolicy: AddressingPolicy = {
    decide(): AddressingDecision {
        return { respond: true, reason: 'http-qa always answers' };
    },
};

describe('channel 接入契约 — 用假想 HTTP 问答 channel 验证不被 IM 绑架', () => {
    it('InboundAdapter.parse 产出合法的、无 IM 假设的 InboundMessage', () => {
        const msg = httpInbound.parse({ qid: 'q1', user: 'u1', question: 'hello?' });
        // InboundAdapter.parse 契约允许返回 null（拒收非法输入）；这里输入合法，
        // 断言非空既验证行为又向类型系统收窄掉 null 分支。
        expect(msg).not.toBeNull();
        if (msg === null) throw new Error('parse should return a message for valid input');
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

    it('AddressingPolicy 返回带 reason 的决策，不是裸 bool', () => {
        const d = httpPolicy.decide({} as InboundMessage, 'qa-bot');
        expect(d.respond).toBe(true);
        expect(typeof d.reason).toBe('string');
        expect(d.reason.length).toBeGreaterThan(0);
    });
});

// ---- 一个 IM 风格的 AddressingPolicy：验证 direct/group 行为且"不响应必有理由" ----

const imPolicy: AddressingPolicy = {
    decide(msg: InboundMessage, botMentionTarget: string): AddressingDecision {
        if (msg.conversation_scope === 'direct') {
            return { respond: true, reason: 'direct message' };
        }
        const hit = msg.addressing_hints.some((h) => h.targetId === botMentionTarget);
        return hit
            ? { respond: true, reason: 'bot addressed in group' }
            : { respond: false, reason: 'group message not addressed to this bot' };
    },
};

const baseMsg: InboundMessage = {
    channel: 'channel-x',
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

describe('图片项：渠道引用和对象位置各占一格', () => {
    // key 是渠道内能解析回原图的引用（飞书的 image_key、QQ 的来源地址），object 是这张
    // 图在对象存储里的位置。读取侧只认 object —— 它按渠道口径去猜的那一天，飞书那套
    // 命名套到 QQ 上当场就错。

    function withImage(image: ContentItem): unknown {
        return { ...baseMsg, content: [image] };
    }

    it('图片项带上 object 之后仍然是合法消息', () => {
        expect(() =>
            assertValidInboundMessage(
                withImage({ kind: 'image', key: 'img_v3_aa', object: 'temp/img_v3_aa.jpg' }),
            ),
        ).not.toThrow();
    });

    it('没有 object 的图片项也合法：这张图还没进对象存储，如实缺席', () => {
        expect(() =>
            assertValidInboundMessage(withImage({ kind: 'image', key: 'img_v3_aa' })),
        ).not.toThrow();
    });

    it('object 是空串就抛：写一格指不到任何对象的位置，比不写更糟', () => {
        // 空串在读取侧跟"没有这一格"读起来一样，但它会让"写入方说过这张图在哪"这句话
        // 变成假的 —— 而整条链的前提正是这句话为真。
        expect(() =>
            assertValidInboundMessage(withImage({ kind: 'image', key: 'k', object: '  ' })),
        ).toThrow();
    });

    it('object 不是字符串也抛', () => {
        expect(() =>
            assertValidInboundMessage(
                withImage({ kind: 'image', key: 'k', object: 42 } as unknown as ContentItem),
            ),
        ).toThrow();
    });
});

describe('summarizeContent — 给人看的那一行摘要', () => {
    // 消息列表、日志、后台读的都是它。入站和出站共用这一份：两边各写一遍的话，同一条
    // 带图的消息在两个方向上摘出来的样子不一样，而它们本该长成同一个样子。

    it('文字原样，别的片段一律折成 [kind]', () => {
        expect(
            summarizeContent([
                { kind: 'text', text: '看这张' },
                { kind: 'image', key: 'img_1', object: 'temp/img_1.jpg' },
            ]),
        ).toBe('看这张[image]');
    });

    it('unsupported 摘的是给人看的占位串，不是类型名', () => {
        expect(summarizeContent([{ kind: 'unsupported', text: '[合并转发]' }])).toBe('[合并转发]');
    });

    it('一个字都没有时给空串，由调用方决定写不写这一列', () => {
        expect(summarizeContent([{ kind: 'text', text: '  ' }])).toBe('');
        expect(summarizeContent([])).toBe('');
    });
});

describe('enforceDecision — 把“不响应必带可记录 reason”从约定变成强制', () => {
    it('respond=true：返回 true，不记日志', () => {
        const logs: string[] = [];
        const go = enforceDecision({ respond: true, reason: 'direct' }, (r) => logs.push(r));
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
        expect(() => enforceDecision({ respond: false, reason: '   ' }, () => {})).toThrow();
    });
});
