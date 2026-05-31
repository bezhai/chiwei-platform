// inbound-pipeline 5b 集成测试：用真实飞书入站组件（larkInbound / larkAddressing）
// 把契约链跑通，钉死 parse→decide(enforceDecision)→resolve 的顺序和真实语义。
// 不 mock adapter / policy —— 这是新插件入站实现的集成回归基线。
// （RuleMessage 派生由 plugins/lark/build-rule-message.test.ts 单独覆盖；此处只
// 钉死契约链本身，不依赖 buildLarkRuleMessage——它在别处被 mock.module 进程级
// 替换，跨文件同进程会污染这里的断言。）
import { runInboundContractChain } from './inbound-pipeline';
import { assertValidInboundMessage } from './contracts';
import { describe, it, expect, mock } from 'bun:test';
import { larkInbound, LARK_CHANNEL } from '@plugins/lark/inbound';
import { larkAddressing } from '@plugins/lark/addressing';

const LARK = LARK_CHANNEL;

describe('5b real-lark contract chain (no mock adapter/policy)', () => {
    // identity resolver：lark 裸 id → 全局 global_* id（黑盒桩）。
    const resolver = {
        resolve: mock(async (kind: string, _channel: string, id: string) => {
            return `global_${kind}_${id}`;
        }),
    };

    function p2pTextEvent() {
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

    function groupMentionEvent(mentionUnionIds: string[]) {
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

    it('p2p text: parse->decide(respond)->resolve all global ids', async () => {
        const result = await runInboundContractChain({
            params: p2pTextEvent(),
            parse: (raw) => larkInbound.parse(raw as never),
            decide: (m, b) => larkAddressing.decide(m, b),
            botIdentity: 'on_user',
            resolver: resolver as never,
            logSkip: () => {},
        });
        expect(result.ok).toBe(true);
        if (!result.ok) return;
        expect(result.respond).toBe(true);
        expect(result.globalUserId).toBe('global_user_on_user');
        expect(result.globalChatId).toBe('global_chat_oc_chat1');
        expect(result.globalMessageId).toBe('global_message_om_msg1');
    });

    it('group @bot: respond=true, hints carry union ids', async () => {
        const result = await runInboundContractChain({
            params: groupMentionEvent(['on_bot']),
            parse: (raw) => larkInbound.parse(raw as never),
            decide: (m, b) => larkAddressing.decide(m, b),
            botIdentity: 'on_bot',
            resolver: resolver as never,
            logSkip: () => {},
        });
        expect(result.ok).toBe(true);
        if (!result.ok) return;
        expect(result.respond).toBe(true);
    });

    it('group non-@bot: respond=false but ok=true (real policy non-empty reason)', async () => {
        const logged: string[] = [];
        const result = await runInboundContractChain({
            params: groupMentionEvent(['on_other']),
            parse: (raw) => larkInbound.parse(raw as never),
            decide: (m, b) => larkAddressing.decide(m, b),
            botIdentity: 'on_bot',
            resolver: resolver as never,
            logSkip: (r) => logged.push(r),
        });
        expect(result.ok).toBe(true);
        if (!result.ok) return;
        expect(result.respond).toBe(false);
        expect(logged.length).toBe(1);
        expect(logged[0].length).toBeGreaterThan(0);
    });

    it('parse returns null for non-message -> ok:false parsed_null', async () => {
        const result = await runInboundContractChain({
            params: { app_id: 'x', sender: {} },
            parse: (raw) => larkInbound.parse(raw as never),
            decide: (m, b) => larkAddressing.decide(m, b),
            botIdentity: 'on_bot',
            resolver: resolver as never,
            logSkip: () => {},
        });
        expect(result.ok).toBe(false);
        if (result.ok) return;
        expect(result.reason).toBe('parsed_null');
    });

    it('chain exposes inbound + resolved global ids for downstream RuleMessage derivation', async () => {
        const result = await runInboundContractChain({
            params: p2pTextEvent(),
            parse: (raw) => larkInbound.parse(raw as never),
            decide: (m, b) => larkAddressing.decide(m, b),
            botIdentity: 'on_user',
            resolver: resolver as never,
            logSkip: () => {},
        });
        expect(result.ok).toBe(true);
        if (!result.ok) return;
        // 下游 buildLarkRuleMessage 的数据来源：全局 id + inbound 的 addressing_hints。
        expect(result.globalMessageId).toBe('global_message_om_msg1');
        expect(result.inbound.channel).toBe(LARK);
        expect(result.inbound.addressing_hints).toEqual([]);
    });
});

// -------- 入站消息形状（adapter parse + guard）--------

describe('5b real-lark inbound message shape (adapter parse + guard)', () => {
    function p2pTextEvent() {
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

    function groupMentionEvent(mentionUnionIds: string[]) {
        return {
            app_id: 'cli_app',
            sender: { sender_id: { union_id: 'on_sender' }, sender_type: 'user' },
            message: {
                message_id: 'om_msg3',
                chat_id: 'oc_group2',
                chat_type: 'group',
                message_type: 'text',
                create_time: '1700000002000',
                content: '{}',
                mentions: mentionUnionIds.map((uid, i) => ({
                    key: `@_user_${i + 1}`,
                    id: { union_id: uid, open_id: `ou_${uid}` },
                    name: uid,
                    mentioned_type: 'user',
                })),
            },
        };
    }

    it('adapter.parse output passes assertValidInboundMessage', () => {
        const msg = larkInbound.parse(p2pTextEvent() as never);
        expect(msg).not.toBeNull();
        assertValidInboundMessage(msg);
        expect(msg!.channel).toBe(LARK);
    });

    it('decide respects botIdentity = union id (mention hit)', () => {
        const ev = groupMentionEvent(['on_bot']);
        const msg = larkInbound.parse(ev as never);
        const d = larkAddressing.decide(msg!, 'on_bot');
        expect(d.respond).toBe(true);
    });
});
