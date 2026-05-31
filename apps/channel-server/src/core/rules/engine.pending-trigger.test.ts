import { describe, it, expect } from 'bun:test';

import { runRulesWith, type PendingChatTrigger } from './engine';
import type { RuleConfig } from './rule';
import type { RuleMessage } from './rule-message';

// 5b 入站重排（决策一）：handler 在 runRules 阶段不实际 publish，而是把
// "待发 ChatTrigger 意图"登记，引擎把它折进唯一终态 RuleTerminalState
// 的可选字段 pendingChatTrigger（与决策四单一终态出口同构、类型强制、
// 并发安全 —— 每次 runRulesWith 调用一个本地 capture，不引入模块级
// 可变 outbox）。调用方据 terminal.pendingChatTrigger 决定 storeMessage
// 成功后是否发 MQ。本测试钉死：
//
//   handler 调 ctx.registerPendingChatTrigger → 终态 responded 且
//   terminal.pendingChatTrigger 带回该意图；
//   未登记的路径（blocked / no_match / handler_error）pendingChatTrigger
//   为 undefined（绝不凭空造发送意图）。

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

const alwaysPass = () => true;

const fakePending: PendingChatTrigger = {
    payload: {
        session_id: 's',
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
    dedupeKey: 'make_reply:M1',
    savePending: async () => undefined,
};

describe('runRules folds pending ChatTrigger into the single terminal state', () => {
    it('handler registers pending trigger -> responded terminal carries pendingChatTrigger', async () => {
        const rules: RuleConfig[] = [
            {
                rules: [alwaysPass],
                handler: async (_m, ctx) => {
                    ctx?.registerPendingChatTrigger(fakePending);
                },
                comment: '聊天',
                category: 'persona',
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
        expect(st.pendingChatTrigger!.dedupeKey).toBe('make_reply:M1');
        expect(st.pendingChatTrigger!.payload.message_id).toBe('M1');
    });

    it('blocked terminal carries no pendingChatTrigger', async () => {
        const rules: RuleConfig[] = [
            {
                rules: [alwaysPass],
                handler: async (_m, ctx) => ctx?.registerPendingChatTrigger(fakePending),
                comment: '聊天',
            },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => false,
        });
        expect(st.kind).toBe('blocked');
        expect(st.pendingChatTrigger).toBeUndefined();
    });

    it('no_match terminal carries no pendingChatTrigger', async () => {
        const rules: RuleConfig[] = [
            { rules: [() => false], handler: async () => {}, comment: 'miss' },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('no_match');
        expect(st.pendingChatTrigger).toBeUndefined();
    });

    it('handler throws after NOT registering -> handler_error, no pendingChatTrigger', async () => {
        const rules: RuleConfig[] = [
            {
                rules: [alwaysPass],
                handler: async () => {
                    throw new Error('boom');
                },
                comment: '聊天',
            },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('handler_error');
        expect(st.pendingChatTrigger).toBeUndefined();
    });

    it('utility fallthrough rule that does NOT register -> no pendingChatTrigger leaked', async () => {
        const rules: RuleConfig[] = [
            {
                rules: [alwaysPass],
                handler: async () => {},
                comment: '复读功能',
                fallthrough: true,
            },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('responded');
        expect(st.pendingChatTrigger).toBeUndefined();
    });
});
