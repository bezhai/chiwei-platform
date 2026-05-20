import { describe, it, expect, mock } from 'bun:test';

import {
    assertValidInboundMessage,
    enforceDecision,
    type InboundAdapter,
    type InboundMessage,
} from '../contracts';

import {
    LarkInboundAdapter,
    LarkOutboundAdapter,
    LarkAddressingPolicy,
    type LarkSendTransport,
} from './lark-adapter';
import type { LarkReceiveMessage } from 'types/lark';

const LARK = 'lark';

function p2pTextEvent(): LarkReceiveMessage {
    return {
        app_id: 'cli_app',
        sender: { sender_id: { union_id: 'on_user', open_id: 'ou_user' }, sender_type: 'user' },
        message: {
            message_id: 'om_msg1',
            chat_id: 'oc_chat1',
            chat_type: 'p2p',
            message_type: 'text',
            create_time: '1700000000000',
            content: JSON.stringify({ text: 'hello bot' }),
        },
    };
}

function groupMentionEvent(mentionUnionIds: string[]): LarkReceiveMessage {
    return {
        app_id: 'cli_app',
        sender: { sender_id: { union_id: 'on_sender', open_id: 'ou_sender' }, sender_type: 'user' },
        message: {
            message_id: 'om_msg2',
            chat_id: 'oc_group',
            chat_type: 'group',
            message_type: 'text',
            create_time: '1700000001000',
            root_id: 'om_root',
            parent_id: 'om_parent',
            content: JSON.stringify({ text: '@_user_1 hi' }),
            mentions: mentionUnionIds.map((uid, i) => ({
                key: `@_user_${i + 1}`,
                id: { union_id: uid, open_id: `ou_${uid}` },
                name: uid,
                mentioned_type: 'user',
            })),
        },
    };
}

describe('LarkInboundAdapter.parse', () => {
    const adapter = new LarkInboundAdapter();

    it('maps p2p text event to a valid InboundMessage with scope=direct', async () => {
        const msg = (await adapter.parse(p2pTextEvent())) as InboundMessage;
        expect(msg).not.toBeNull();
        assertValidInboundMessage(msg);
        expect(msg.channel).toBe(LARK);
        expect(msg.channel_message_id).toBe('om_msg1');
        expect(msg.channel_chat_id).toBe('oc_chat1');
        expect(msg.channel_user_id).toBe('on_user');
        expect(msg.conversation_scope).toBe('direct');
        // 缺陷2：入站消息自身就是回复锚点（复刻 replyMessage(messageId,...,true)）。
        expect(msg.thread_ref).toEqual({
            selfChannelMessageId: 'om_msg1',
            inThread: true,
        });
        expect(msg.addressing_hints).toEqual([]);
        expect(msg.content).toEqual([{ kind: 'text', text: 'hello bot' }]);
        expect(msg.received_at).toBe(1700000000000);
    });

    it('maps group event scope=group, mentions->addressing_hints(union_id), thread_ref from root/parent', async () => {
        const msg = (await adapter.parse(groupMentionEvent(['on_bot', 'on_other']))) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.conversation_scope).toBe('group');
        expect(msg.addressing_hints).toEqual([{ targetId: 'on_bot' }, { targetId: 'on_other' }]);
        expect(msg.thread_ref).toEqual({
            selfChannelMessageId: 'om_msg2',
            replyToChannelMessageId: 'om_parent',
            rootChannelMessageId: 'om_root',
            inThread: true,
        });
    });

    // 缺陷1 回归守卫：飞书现状里赤尾会处理图片/富文本/sticker/media/file/audio/
    // 合并转发/分享名片/unsupported，绝不能因为接 channel 契约就把这些当没收到。
    // 映射口径必须与现状 MessageTransferer/MessageContent 一致。
    function p2pEventOfType(messageType: string, content: unknown): LarkReceiveMessage {
        const ev = p2pTextEvent();
        ev.message.message_type = messageType;
        ev.message.content = JSON.stringify(content);
        return ev;
    }

    it('image message -> image content item (parse never returns null for non-text)', async () => {
        const msg = (await adapter.parse(
            p2pEventOfType('image', { image_key: 'img_x' }),
        )) as InboundMessage;
        expect(msg).not.toBeNull();
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([{ kind: 'image', key: 'img_x' }]);
    });

    it('sticker message -> sticker content item', async () => {
        const msg = (await adapter.parse(
            p2pEventOfType('sticker', { file_key: 'stk_1' }),
        )) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([{ kind: 'sticker', key: 'stk_1' }]);
    });

    it('post (rich text) message -> mixed text/image items, mirrors MessageTransferer', async () => {
        const post = {
            content: [
                [
                    { tag: 'text', text: 'hello ' },
                    { tag: 'img', image_key: 'img_in_post' },
                ],
                [{ tag: 'text', text: 'world' }],
            ],
        };
        const msg = (await adapter.parse(p2pEventOfType('post', post))) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            { kind: 'text', text: 'hello ' },
            { kind: 'image', key: 'img_in_post' },
            { kind: 'text', text: 'world' },
        ]);
    });

    it('post with no renderable nodes -> placeholder text [富文本]', async () => {
        const msg = (await adapter.parse(
            p2pEventOfType('post', { content: [[{ tag: 'a', href: 'x' }]] }),
        )) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([{ kind: 'text', text: '[富文本]' }]);
    });

    it('media message -> file content item carrying media meta', async () => {
        const msg = (await adapter.parse(
            p2pEventOfType('media', {
                file_key: 'mf_1',
                image_key: 'cover',
                file_name: 'v.mp4',
                duration: 12,
            }),
        )) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            {
                kind: 'file',
                key: 'mf_1',
                meta: { image_key: 'cover', file_name: 'v.mp4', duration: 12, lark_type: 'media' },
            },
        ]);
    });

    it('file message -> file content item carrying file meta', async () => {
        const msg = (await adapter.parse(
            p2pEventOfType('file', { file_key: 'fk_1', file_name: 'a.pdf' }),
        )) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            { kind: 'file', key: 'fk_1', meta: { file_name: 'a.pdf', lark_type: 'file' } },
        ]);
    });

    it('audio message -> audio content item carrying duration meta', async () => {
        const msg = (await adapter.parse(
            p2pEventOfType('audio', { file_key: 'au_1', duration: 5 }),
        )) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            { kind: 'audio', key: 'au_1', meta: { duration: 5 } },
        ]);
    });

    it('merge_forward / share_chat / share_user -> unsupported item with original type', async () => {
        const mf = (await adapter.parse(p2pEventOfType('merge_forward', {}))) as InboundMessage;
        assertValidInboundMessage(mf);
        expect(mf.content).toEqual([
            { kind: 'unsupported', text: '[合并转发]', meta: { original_type: 'merge_forward' } },
        ]);

        const sc = (await adapter.parse(
            p2pEventOfType('share_chat', { chat_id: 'oc_x' }),
        )) as InboundMessage;
        assertValidInboundMessage(sc);
        expect(sc.content).toEqual([
            { kind: 'unsupported', text: '[分享群名片]', meta: { original_type: 'share_chat' } },
        ]);

        const su = (await adapter.parse(
            p2pEventOfType('share_user', { user_id: 'ou_x' }),
        )) as InboundMessage;
        assertValidInboundMessage(su);
        expect(su.content).toEqual([
            { kind: 'unsupported', text: '[分享个人名片]', meta: { original_type: 'share_user' } },
        ]);
    });

    it('unknown message type -> unsupported item, never null', async () => {
        const msg = (await adapter.parse(p2pEventOfType('calendar', {}))) as InboundMessage;
        expect(msg).not.toBeNull();
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            { kind: 'unsupported', text: '[calendar]', meta: { original_type: 'calendar' } },
        ]);
    });

    it('still returns null when there is no message at all (not a parseable event)', async () => {
        expect(await adapter.parse({} as LarkReceiveMessage)).toBeNull();
    });

    // 缺陷2 回归守卫：飞书入站消息自身必须可作回复锚点，使 deliver/reply 复刻
    // replyMessage(message.messageId, content, replyInThread=true) 现状语义。
    it('thread_ref carries the message itself as a reply anchor + inThread=true', async () => {
        const msg = (await adapter.parse(p2pTextEvent())) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.thread_ref).toEqual({
            selfChannelMessageId: 'om_msg1',
            inThread: true,
        });
    });

    it('group thread_ref keeps root/parent AND the self anchor + inThread', async () => {
        const msg = (await adapter.parse(
            groupMentionEvent(['on_bot']),
        )) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.thread_ref).toEqual({
            selfChannelMessageId: 'om_msg2',
            replyToChannelMessageId: 'om_parent',
            rootChannelMessageId: 'om_root',
            inThread: true,
        });
    });

    it('handleHandshake returns challenge for url_verification, null otherwise', () => {
        expect(
            adapter.handleHandshake({ type: 'url_verification', challenge: 'abc', token: 't' }),
        ).toEqual({ challenge: 'abc' });
        expect(adapter.handleHandshake(p2pTextEvent())).toBeNull();
    });

    // 必改1：parse 的契约签名是同步 InboundMessage|null。从 InboundAdapter 接口
    // 类型（而非具体 LarkInboundAdapter 类）调用，结果必须是已解析的 InboundMessage
    // 本体，不能是 Promise。这钉死接口级签名一致，防 T5 接线时拿到 Promise。
    it('parse is synchronous at the InboundAdapter interface level (not a Promise)', () => {
        const ia: InboundAdapter = new LarkInboundAdapter();
        const result = ia.parse(p2pTextEvent());
        // 直接拿到对象本体，不是 thenable
        expect(result).not.toBeNull();
        expect(typeof (result as { then?: unknown }).then).not.toBe('function');
        assertValidInboundMessage(result);
        expect((result as InboundMessage).channel_message_id).toBe('om_msg1');
        // 非可解析事件同样同步返回 null
        expect(ia.parse({} as LarkReceiveMessage)).toBeNull();
    });

    // 建议1：飞书 media(视频) 折叠成 kind:'file' 后，视频语义只靠 meta.lark_type
    // === 'media' 保留。钉住这个标记，确保后续渲染/转旧模型能还原"视频"占位语义。
    it('media keeps video semantics via meta.lark_type==="media" after folding to file', () => {
        const msg = adapter.parse(
            p2pEventOfType('media', { file_key: 'mf_2', file_name: 'clip.mp4', duration: 7 }),
        ) as InboundMessage;
        assertValidInboundMessage(msg);
        const item = msg.content[0] as { kind: string; meta?: Record<string, unknown> };
        expect(item.kind).toBe('file');
        // 视频语义不丢：lark_type 标记把它和普通 file 区分开
        expect(item.meta?.lark_type).toBe('media');
        const fileMsg = adapter.parse(
            p2pEventOfType('file', { file_key: 'fk_2', file_name: 'doc.pdf' }),
        ) as InboundMessage;
        const fileItem = fileMsg.content[0] as { meta?: Record<string, unknown> };
        expect(fileItem.meta?.lark_type).toBe('file');
        expect(fileItem.meta?.lark_type).not.toBe(item.meta?.lark_type);
    });
});

// 必改2：assertValidInboundMessage 在 thread_ref 非 null 时必须要求至少一个
// 锚点字段为非空字符串；空对象 { thread_ref: {} } 会让 deliver→reply 把回复
// 目标解析成空字符串，违反 spec"禁止静默"，必须在入站边界炸。
describe('assertValidInboundMessage thread_ref anchor guard', () => {
    function baseMsg(): Record<string, unknown> {
        return {
            channel: 'lark',
            bot_name: 'b',
            channel_message_id: 'm',
            channel_chat_id: 'c',
            channel_user_id: 'u',
            conversation_scope: 'direct',
            thread_ref: null,
            addressing_hints: [],
            content: [{ kind: 'text', text: 'x' }],
            received_at: 0,
        };
    }

    it('rejects thread_ref that is an object with no anchor (empty {})', () => {
        const m = { ...baseMsg(), thread_ref: {} };
        expect(() => assertValidInboundMessage(m)).toThrow(/anchor/i);
    });

    it('rejects thread_ref whose only anchor fields are empty strings', () => {
        const m = {
            ...baseMsg(),
            thread_ref: { selfChannelMessageId: '', replyToChannelMessageId: '' },
        };
        expect(() => assertValidInboundMessage(m)).toThrow(/anchor/i);
    });

    it('accepts thread_ref with a non-empty self anchor', () => {
        const m = { ...baseMsg(), thread_ref: { selfChannelMessageId: 'om_1', inThread: true } };
        expect(() => assertValidInboundMessage(m)).not.toThrow();
    });

    it('accepts thread_ref with only a non-empty root anchor', () => {
        const m = { ...baseMsg(), thread_ref: { rootChannelMessageId: 'om_root' } };
        expect(() => assertValidInboundMessage(m)).not.toThrow();
    });

    it('still accepts thread_ref === null (no reply semantics)', () => {
        expect(() => assertValidInboundMessage(baseMsg())).not.toThrow();
    });

    it('inThread alone (no message-id anchor) is not a valid anchor -> throws', () => {
        const m = { ...baseMsg(), thread_ref: { inThread: true } };
        expect(() => assertValidInboundMessage(m)).toThrow(/anchor/i);
    });
});

describe('LarkOutboundAdapter', () => {
    // 注入假 transport，验证 OutboundAdapter 把通用契约调用翻译成现有飞书
    // 纯文本发送/回复（参数 { text } + msg_type 'text'）、并回传 message_id。
    const sendMock = mock(async () => ({ message_id: 'om_sent' }));
    const replyMock = mock(async () => ({ message_id: 'om_replied' }));
    const transport: LarkSendTransport = { send: sendMock, reply: replyMock };
    const adapter = new LarkOutboundAdapter(transport);

    it('send wraps lark send path and returns channel_message_id', async () => {
        const id = await adapter.send('oc_chat1', 'hello');
        expect(id).toBe('om_sent');
        expect(sendMock).toHaveBeenCalledWith('oc_chat1', { text: 'hello' }, 'text');
    });

    // 缺陷2：飞书现状回复链路是 replyMessage(message.messageId, content, true)
    // —— 回复触发那条消息本身、且在话题内。reply 必须忠实表达这两点。
    it('reply targets the message itself (selfChannelMessageId) with reply_in_thread=true', async () => {
        const id = await adapter.reply(
            {
                selfChannelMessageId: 'om_msg1',
                replyToChannelMessageId: 'om_parent',
                rootChannelMessageId: 'om_root',
                inThread: true,
            },
            'hi back',
        );
        expect(id).toBe('om_replied');
        // 回复触发消息本身（om_msg1），并把 reply_in_thread 传成 true。
        expect(replyMock).toHaveBeenCalledWith('om_msg1', { text: 'hi back' }, 'text', true);
    });

    it('reply without inThread falls back to a non-thread reply (replyInThread=false)', async () => {
        replyMock.mockClear();
        await adapter.reply({ selfChannelMessageId: 'om_msg1' }, 'x');
        expect(replyMock).toHaveBeenCalledWith('om_msg1', { text: 'x' }, 'text', false);
    });

    it('reply without a self anchor falls back to root/parent (legacy reply tree)', async () => {
        replyMock.mockClear();
        await adapter.reply(
            { replyToChannelMessageId: 'om_parent', rootChannelMessageId: 'om_root' },
            'y',
        );
        expect(replyMock).toHaveBeenCalledWith('om_parent', { text: 'y' }, 'text', false);
    });
});

describe('LarkAddressingPolicy (logical equivalence to NeedRobotMention)', () => {
    const policy = new LarkAddressingPolicy();
    const BOT = 'on_bot';

    function inbound(scope: string, hints: string[]): InboundMessage {
        return {
            channel: LARK,
            bot_name: 'b',
            channel_message_id: 'm',
            channel_chat_id: 'c',
            channel_user_id: 'u',
            conversation_scope: scope,
            thread_ref: null,
            addressing_hints: hints.map((targetId) => ({ targetId })),
            content: [{ kind: 'text', text: 'x' }],
            received_at: 0,
        };
    }

    it('direct -> respond=true (isP2P branch), reason non-empty', () => {
        const d = policy.decide(inbound('direct', []), BOT);
        expect(d.respond).toBe(true);
        expect(d.reason.trim().length).toBeGreaterThan(0);
    });

    it('group with bot mentioned -> respond=true, reason non-empty', () => {
        const d = policy.decide(inbound('group', ['on_other', BOT]), BOT);
        expect(d.respond).toBe(true);
        expect(d.reason.trim().length).toBeGreaterThan(0);
    });

    it('group without bot mention -> respond=false, reason non-empty (no silent drop)', () => {
        const d = policy.decide(inbound('group', ['on_other']), BOT);
        expect(d.respond).toBe(false);
        expect(d.reason.trim().length).toBeGreaterThan(0);
        // enforceDecision must not throw -> reason carries the why
        const logged: string[] = [];
        expect(enforceDecision(d, (r) => logged.push(r))).toBe(false);
        expect(logged.length).toBe(1);
    });

    // 缺陷3：addressing_hints[].targetId 取自 mentions[].id.union_id（见
    // LarkInboundAdapter.parse），所以 botIdentity 必须传飞书 robot_union_id 才
    // 与现状 NeedRobotMention 等价。传 app_id / open_id / bot_name 必然命不中
    // -> 群里 @bot 不响应。守住这条口径，防止接线误传。
    it('hint targetIds are lark robot union_ids; botIdentity MUST be the robot union_id', async () => {
        const adapter = new LarkInboundAdapter();
        const ev = groupMentionEvent(['on_bot_union']);
        const m = (await adapter.parse(ev)) as InboundMessage;
        // adapter 产出的 hint 用 union_id 口径
        expect(m.addressing_hints).toEqual([{ targetId: 'on_bot_union' }]);
        // 传 union_id：命中
        expect(policy.decide(m, 'on_bot_union').respond).toBe(true);
        // 传 app_id / open_id / bot_name：必不命中（这正是要防的误传）
        expect(policy.decide(m, 'cli_app').respond).toBe(false);
        expect(policy.decide(m, 'ou_on_bot_union').respond).toBe(false);
        expect(policy.decide(m, 'bot_display_name').respond).toBe(false);
    });
});
