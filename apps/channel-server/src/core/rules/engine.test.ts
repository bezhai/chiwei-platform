import { describe, it, expect } from 'bun:test';

import { runRulesWith, type RuleTerminalState } from './engine';
import type { RuleConfig } from './rule';
import type { RuleMessage } from './rule-message';

// 决策四的核心：runRules 单一终态出口。每一条进入 runRules 的消息，无论走哪条
// 退出路径（黑名单挡掉 / channel 过滤跳过 / 异步规则 false / botRole 不匹配 /
// handler 抛异常 / fallthrough / 无任何规则匹配），都必须收敛到一个唯一、明确、
// 可查的终态记录。禁止任何无终态记录的静默 break/return。
//
// runRulesWith 是 runRules 的可注入内核：注入 chatRules / botRole / NotBlocked，
// 不连真实 multiBotManager / DB，纯跑。返回唯一 RuleTerminalState。

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

describe('runRules single terminal-state exit', () => {
    it('NotBlocked=false -> exactly one terminal state: blocked', async () => {
        let handled = false;
        const rules: RuleConfig[] = [
            { rules: [alwaysPass], handler: async () => { handled = true; }, comment: 'chat' },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => false,
        });
        expect(st.kind).toBe('blocked');
        expect(handled).toBe(false);
        expect(st.messageId).toBe('M1');
        expect(st.channel).toBe('lark');
    });

    it('a matching rule that responds -> terminal state: responded with the matched comment', async () => {
        const rules: RuleConfig[] = [
            { rules: [() => false], handler: async () => {}, comment: 'miss' },
            { rules: [alwaysPass], handler: async () => {}, comment: '聊天' },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('responded');
        expect(st.matchedRule).toBe('聊天');
    });

    it('handler throws -> terminal state: handler_error, never silently swallowed', async () => {
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
        expect(st.matchedRule).toBe('聊天');
        expect(st.detail).toContain('boom');
    });

    it('no rule matches -> terminal state: no_match (not a silent return)', async () => {
        const rules: RuleConfig[] = [
            { rules: [() => false], handler: async () => {}, comment: 'a' },
            { rules: [() => false], handler: async () => {}, comment: 'b' },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('no_match');
    });

    it('async_rules false -> rule skipped, still收敛到 no_match terminal state', async () => {
        const rules: RuleConfig[] = [
            {
                rules: [alwaysPass],
                async_rules: [async () => false],
                handler: async () => {},
                comment: 'meme',
            },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('no_match');
    });
});

// 决策四漏洞收口：规则"执行阶段本身"抛异常（notBlocked 调用抛错 / sync rule
// 谓词抛错 / async rule reject）也必须收敛到唯一可查终态，绝不裸逃出 runRulesWith
// 绕过 logTerminalState。捕获 ≠ 吞错：终态必须可查到哪条消息哪阶段什么异常。
describe('runRules 规则执行阶段异常也收敛到单一终态', () => {
    it('notBlocked throws -> terminal state rule_error, no exception escapes', async () => {
        let handled = false;
        const rules: RuleConfig[] = [
            { rules: [alwaysPass], handler: async () => { handled = true; }, comment: 'chat' },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => {
                throw new Error('blacklist db down');
            },
        });
        expect(st.kind).toBe('rule_error');
        expect(handled).toBe(false);
        expect(st.matchedRule).toContain('notBlocked');
        expect(st.detail).toContain('blacklist db down');
        expect(st.messageId).toBe('M1');
        expect(st.channel).toBe('lark');
    });

    it('sync rule predicate throws -> terminal state rule_error pointing at the rule', async () => {
        const rules: RuleConfig[] = [
            {
                rules: [
                    () => {
                        throw new Error('sync predicate boom');
                    },
                ],
                handler: async () => {},
                comment: '复读功能',
            },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('rule_error');
        expect(st.matchedRule).toContain('复读功能');
        expect(st.detail).toContain('sync predicate boom');
    });

    it('async rule rejects -> terminal state rule_error pointing at the rule', async () => {
        const rules: RuleConfig[] = [
            {
                rules: [alwaysPass],
                async_rules: [
                    async () => {
                        throw new Error('async rule reject');
                    },
                ],
                handler: async () => {},
                comment: 'Meme',
            },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('rule_error');
        expect(st.matchedRule).toContain('Meme');
        expect(st.detail).toContain('async rule reject');
    });
});

describe('runRules channel filtering + 渠道声明', () => {
    it('QQ message misses a lark-only rule and收敛 to no_match (recorded skip)', async () => {
        let handled = false;
        const rules: RuleConfig[] = [
            {
                rules: [alwaysPass],
                handler: async () => { handled = true; },
                comment: '帮助',
                channels: ['lark'],
            },
        ];
        const st = await runRulesWith(msg({ channel: 'qq' }), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(handled).toBe(false);
        expect(st.kind).toBe('no_match');
        // 被 channel 过滤跳过的指令必须并入终态记录的 skipped 列表
        expect(st.skipped.some((s) => s.includes('帮助'))).toBe(true);
    });

    it('rule with no channels declared defaults to all-platform (persona main path)', async () => {
        const rules: RuleConfig[] = [
            { rules: [alwaysPass], handler: async () => {}, comment: '聊天', category: 'persona' },
        ];
        const st = await runRulesWith(msg({ channel: 'qq' }), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('responded');
        expect(st.matchedRule).toBe('聊天');
    });

    it('lark message still hits a lark-only rule', async () => {
        const rules: RuleConfig[] = [
            { rules: [alwaysPass], handler: async () => {}, comment: '帮助', channels: ['lark'] },
        ];
        const st = await runRulesWith(msg({ channel: 'lark' }), {
            chatRules: rules,
            botRole: undefined,
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('responded');
        expect(st.matchedRule).toBe('帮助');
    });
});

describe('runRules botRole/category filtering收敛到终态', () => {
    it('utility bot skipping persona rule -> no_match terminal state, not silent', async () => {
        const rules: RuleConfig[] = [
            { rules: [alwaysPass], handler: async () => {}, comment: '聊天', category: 'persona' },
        ];
        const st = await runRulesWith(msg(), {
            chatRules: rules,
            botRole: 'utility',
            notBlocked: async () => true,
        });
        expect(st.kind).toBe('no_match');
        expect(st.skipped.some((s) => s.includes('聊天'))).toBe(true);
    });
});
