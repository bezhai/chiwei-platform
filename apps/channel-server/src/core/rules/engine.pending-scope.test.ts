import { describe, it, expect } from 'bun:test';

import { runRulesWith, type PendingChatTrigger } from './engine';
import type { RuleConfig } from './rule';
import type { RuleMessage } from './rule-message';

// 建议1：pendingChatTrigger 改成每个 handler 执行作用域内单独捕获，只把
// 本次命中 handler 注册的 pending 绑定到它对应的 terminal，去掉"循环结束
// 用最新 pending 回填"的防御写法。并发安全与单一终态出口语义不变。
//
// 旧实现的缺陷（本测试钉死不再发生）：handlerCtx 是整个 runRulesWith 调用
// 共享的单变量，pendingChatTrigger 残留上一个 handler 注册的值；fallthrough
// 链里靠后的 handler 没注册 pending 时，loop-end 的
// `{ ...lastResponded, pendingChatTrigger }` 会把**前一个** handler 的
// pending 错绑到**后一个** handler 产生的 terminal。

function msg(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'lark',
        botName: 'bot-x',
        internalUserId: 'U1',
        internalChatId: 'C1',
        internalMessageId: 'M1',
        internalRootId: undefined,
        isDirect: true,
        addressedTargetIds: [],
        createTime: 100,
        clearText: () => '',
        text: () => '',
        withMentionText: () => '',
        withoutEmojiText: () => '',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
        ...over,
    };
}

const pass = () => true;

function pendingFor(id: string): PendingChatTrigger {
    return {
        payload: {
            session_id: id,
            channel: 'lark',
            message_id: 'M1',
            chat_id: 'C1',
            is_p2p: true,
            root_id: 'M1',
            user_id: 'U1',
            bot_name: 'bot-x',
            is_canary: false,
            lane: undefined,
            enqueued_at: 0,
            mentions: [],
        },
        lane: undefined,
        dedupeKey: `make_reply:${id}`,
        savePending: async () => undefined,
    };
}

describe('建议1: per-handler-scoped pending capture, no latest-pending fallthrough回填', () => {
    it('earlier fallthrough handler registers pending; later fallthrough handler registers nothing -> later terminal carries NO pending (no stale回填)', async () => {
        const rules: RuleConfig[] = [
            {
                rules: [pass],
                handler: async (_m, ctx) =>
                    ctx?.registerPendingChatTrigger(pendingFor('A')),
                comment: 'A registers pending',
                fallthrough: true,
            },
            {
                rules: [pass],
                handler: async () => {
                    // B 命中且成功，但**不**注册 pending。
                },
                comment: 'B registers nothing',
                fallthrough: true,
            },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        // 终态由最后一个 fallthrough 响应（B）收敛，B 没注册 pending →
        // 终态绝不携带 A 的 pending（旧实现会把 A 的 pending 回填到 B）。
        expect(st.kind).toBe('responded');
        expect(st.matchedRule).toBe('B registers nothing');
        expect(st.pendingChatTrigger).toBeUndefined();
    });

    it('only the matched persona handler that registered its own pending carries it; other terminals do not', async () => {
        const rules: RuleConfig[] = [
            {
                rules: [pass],
                handler: async () => {
                    // utility 复读类：命中、有副作用，但不注册 pending。
                },
                comment: '复读功能',
                fallthrough: true,
            },
            {
                rules: [pass],
                handler: async (_m, ctx) =>
                    ctx?.registerPendingChatTrigger(pendingFor('persona')),
                comment: '聊天',
                category: 'persona',
                // 非 fallthrough：命中即终态，应携带本 handler 注册的 pending。
            },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('responded');
        expect(st.matchedRule).toBe('聊天');
        expect(st.pendingChatTrigger).toBeDefined();
        expect(st.pendingChatTrigger!.payload.session_id).toBe('persona');
    });

    it('persona registers, then a trailing fallthrough handler runs and registers nothing -> terminal carries persona pending only because persona was the producing handler (not stale leak)', async () => {
        // 这个场景钉死：单一终态由最后一个 fallthrough 响应收敛。若最后
        // 响应的 handler 没注册 pending，终态就不带 pending —— 即便链中
        // 更早的 persona handler 注册过。pending 必须与「产生该终态的那个
        // handler」严格绑定，不是"谁都行只要链上有人注册过"。
        const rules: RuleConfig[] = [
            {
                rules: [pass],
                handler: async (_m, ctx) =>
                    ctx?.registerPendingChatTrigger(pendingFor('persona')),
                comment: '聊天',
                category: 'persona',
                fallthrough: true,
            },
            {
                rules: [pass],
                handler: async () => {
                    // trailing utility，命中、不注册 pending。
                },
                comment: 'trailing utility',
                fallthrough: true,
            },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('responded');
        expect(st.matchedRule).toBe('trailing utility');
        // trailing utility 没注册 → 终态不带 pending（旧实现会回填 persona）。
        expect(st.pendingChatTrigger).toBeUndefined();
    });
});
