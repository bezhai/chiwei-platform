import { describe, it, expect } from 'bun:test';

import { runInboundContractChain } from './inbound-pipeline';
import { InMemoryIdentityResolver } from './identity-resolver';
import type { InboundMessage, AddressingPolicy } from './contracts';

// 5b 接线核心：钉死链路顺序
//   adapter.parse → AddressingPolicy.decide+enforceDecision(前置总闸) →
//   IdentityResolver.resolve(换全局 internal_*_id) →
//   产出 globalIds + respond + 终态可查
// fail-loud：契约链(parse/decide/resolve)任一失败 = 不产出 globalIds、
// 调用方据此不写库不发 MQ，绝不退回 channel 裸 ID。

function inbound(scope: string, hints: string[] = []): InboundMessage {
    return {
        channel: 'lark',
        bot_name: 'cli_x',
        channel_message_id: 'lm1',
        channel_chat_id: 'lc1',
        channel_user_id: 'lu1',
        conversation_scope: scope,
        thread_ref: { selfChannelMessageId: 'lm1', inThread: true },
        addressing_hints: hints.map((targetId) => ({ targetId })),
        content: [{ kind: 'text', text: 'hi' }],
        received_at: 123,
    };
}

const policy: AddressingPolicy = {
    decide(msg, botIdentity) {
        if (msg.conversation_scope === 'direct') {
            return { respond: true, reason: 'direct' };
        }
        const mentioned = msg.addressing_hints.some((h) => h.targetId === botIdentity);
        return mentioned
            ? { respond: true, reason: 'mentioned' }
            : { respond: false, reason: 'group without bot mention' };
    },
};

describe('runInboundContractChain pinned order + fail-loud', () => {
    it('parse->decide->resolve in order, produces global ids + respond decision', async () => {
        const calls: string[] = [];
        const resolver = new InMemoryIdentityResolver();
        const res = await runInboundContractChain({
            params: {},
            parse: () => {
                calls.push('parse');
                return inbound('direct');
            },
            decide: (m, b) => {
                calls.push('decide');
                return policy.decide(m, b);
            },
            botIdentity: 'on_bot',
            resolver: {
                resolve: async (...a) => {
                    calls.push('resolve');
                    return resolver.resolve(...a);
                },
                toChannel: resolver.toChannel.bind(resolver),
            },
            logSkip: () => {},
        });
        // decide must come before resolve (DB-independent verdict survives DB failure)
        expect(calls[0]).toBe('parse');
        expect(calls.indexOf('decide')).toBeLessThan(calls.indexOf('resolve'));
        expect(res.ok).toBe(true);
        if (res.ok) {
            expect(res.respond).toBe(true);
            expect(res.globalUserId).toBeTruthy();
            expect(res.globalChatId).toBeTruthy();
            expect(res.globalMessageId).toBeTruthy();
        }
    });

    it('parse returns null (platform misc event) -> ok=false, skipChain, no global ids', async () => {
        const res = await runInboundContractChain({
            params: {},
            parse: () => null,
            decide: policy.decide,
            botIdentity: 'on_bot',
            resolver: new InMemoryIdentityResolver(),
            logSkip: () => {},
        });
        expect(res.ok).toBe(false);
        if (!res.ok) expect(res.reason).toBe('parsed_null');
    });

    it('parse throws -> fail-loud: ok=false, error reason, never raw fallback', async () => {
        const res = await runInboundContractChain({
            params: {},
            parse: () => {
                throw new Error('bad raw event');
            },
            decide: policy.decide,
            botIdentity: 'on_bot',
            resolver: new InMemoryIdentityResolver(),
            logSkip: () => {},
        });
        expect(res.ok).toBe(false);
        if (!res.ok) {
            expect(res.reason).toBe('contract_chain_error');
            expect(res.detail).toContain('bad raw event');
        }
    });

    it('resolver.resolve throws -> fail-loud: ok=false, no global ids leaked', async () => {
        const res = await runInboundContractChain({
            params: {},
            parse: () => inbound('direct'),
            decide: policy.decide,
            botIdentity: 'on_bot',
            resolver: {
                resolve: async () => {
                    throw new Error('PG down');
                },
                toChannel: async () => {
                    throw new Error('na');
                },
            },
            logSkip: () => {},
        });
        expect(res.ok).toBe(false);
        if (!res.ok) {
            expect(res.reason).toBe('contract_chain_error');
            expect(res.detail).toContain('PG down');
        }
    });

    it('decide is called before resolve even though both succeed; empty reason on respond=false throws (enforceDecision)', async () => {
        const res = await runInboundContractChain({
            params: {},
            parse: () => inbound('group', []),
            decide: () => ({ respond: false, reason: '' }),
            botIdentity: 'on_bot',
            resolver: new InMemoryIdentityResolver(),
            logSkip: () => {},
        });
        // empty reason on respond=false is the silent-drop the contract must炸 on
        expect(res.ok).toBe(false);
        if (!res.ok) expect(res.reason).toBe('contract_chain_error');
    });

    it('replyToChannelMessageId present -> resolved into globalReplyToId (loop closure: stored reply_message_id must be global, not raw parentMessageId)', async () => {
        const resolver = new InMemoryIdentityResolver();
        const msg = inbound('direct');
        msg.thread_ref = {
            selfChannelMessageId: 'lm1',
            replyToChannelMessageId: 'lm_parent_raw',
            inThread: true,
        };
        const res = await runInboundContractChain({
            params: {},
            parse: () => msg,
            decide: policy.decide,
            botIdentity: 'on_bot',
            resolver,
            logSkip: () => {},
        });
        expect(res.ok).toBe(true);
        if (res.ok) {
            // 裸 parentMessageId 必须被 resolve 成全局 internal id，
            // 不能把飞书裸 id 直接当 reply_message_id 落库（否则
            // cross_chat.py / _context_messages.py 按全局 PK 关联会失配）。
            expect(res.globalReplyToId).toBeTruthy();
            expect(res.globalReplyToId).not.toBe('lm_parent_raw');
            // 同一裸 id 再次出现命中既有映射（幂等），返回同一全局 id。
            const again = await resolver.resolve('message', 'lark', 'lm_parent_raw');
            expect(res.globalReplyToId).toBe(again);
        }
    });

    it('no replyToChannelMessageId -> globalReplyToId stays undefined (never fabricate an id when there is no parent)', async () => {
        const resolver = new InMemoryIdentityResolver();
        const msg = inbound('direct');
        msg.thread_ref = { selfChannelMessageId: 'lm1', inThread: true };
        const res = await runInboundContractChain({
            params: {},
            parse: () => msg,
            decide: policy.decide,
            botIdentity: 'on_bot',
            resolver,
            logSkip: () => {},
        });
        expect(res.ok).toBe(true);
        if (res.ok) {
            expect(res.globalReplyToId).toBeUndefined();
        }
    });

    it('thread_ref=null -> globalReplyToId undefined (no reply semantics channel)', async () => {
        const resolver = new InMemoryIdentityResolver();
        const msg = inbound('direct');
        msg.thread_ref = null;
        const res = await runInboundContractChain({
            params: {},
            parse: () => msg,
            decide: policy.decide,
            botIdentity: 'on_bot',
            resolver,
            logSkip: () => {},
        });
        expect(res.ok).toBe(true);
        if (res.ok) {
            expect(res.globalReplyToId).toBeUndefined();
        }
    });

    it('group without mention -> respond=false but still resolves global ids (复读 path needs message in runRules; persona path gated separately)', async () => {
        let skipReason = '';
        const res = await runInboundContractChain({
            params: {},
            parse: () => inbound('group', ['someone_else']),
            decide: policy.decide,
            botIdentity: 'on_bot',
            resolver: new InMemoryIdentityResolver(),
            logSkip: (r) => {
                skipReason = r;
            },
        });
        expect(res.ok).toBe(true);
        if (res.ok) {
            expect(res.respond).toBe(false);
            // global ids still resolved: native lark path (复读/storeMessage)
            // must keep working for non-@ group messages — zero lark regression.
            expect(res.globalMessageId).toBeTruthy();
        }
        expect(skipReason).toContain('without bot mention');
    });
});
