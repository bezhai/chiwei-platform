import { describe, it, expect } from 'bun:test';
import { runRulesWith } from './engine';
import type { RuleMessage } from './rule-message';
import type { RuleConfig } from './rule';
import { CommandRegistry } from '@core/registry/command-registry';

// B1 行为契约：指令归属从「engine 硬编码 chatRules + channels flag」改成
// 「谁注册谁拥有」(CommandRegistry)。这里验证 forChannel 产出的指令序列喂进
// runRulesWith 后，分发语义正确：
//   1. 该 channel 注册的平台指令能被命中；
//   2. 核心通用指令(聊天主链路)始终排在最后、作为 catch-all 兜底；
//   3. 另一个 channel(qq)不会命中 lark 注册的平台指令。
// 这是搬家 + 改注册方式后必须保持的不变量——红灯先于实现。

function msg(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'lark',
        botName: 'bot-x',
        commonUserId: 'u1',
        commonConversationId: 'c1',
        commonMessageId: 'm1',
        commonRootMessageId: undefined,
        isDirect: true,
        botCommonUserId: 'BOT-U',
        mentionedUserIds: [],
        createTime: 100,
        clearText: () => 'hi',
        text: () => 'hi',
        withoutEmojiText: () => 'hi',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
        ...over,
    };
}

// 记录被命中的指令 comment，便于断言「命中了哪条」。
function recordingCmd(comment: string, match: boolean, fired: string[]): RuleConfig {
    return {
        rules: [() => match],
        handler: async () => {
            fired.push(comment);
        },
        comment,
    };
}

describe('runRulesWith 经 CommandRegistry.forChannel 分发', () => {
    it('lark 消息：命中 lark 注册的平台指令', async () => {
        const fired: string[] = [];
        const reg = new CommandRegistry();
        reg.register('lark', [recordingCmd('lark-balance', true, fired)]);
        reg.registerCore([recordingCmd('chat', true, fired)]);

        const state = await runRulesWith(msg({ channel: 'lark' }), {
            chatRules: reg.forChannel('lark'),
            botRole: undefined,
            notBlocked: async () => true,
        });

        expect(state.kind).toBe('responded');
        expect(state.matchedRule).toBe('lark-balance');
        expect(fired).toEqual(['lark-balance']);
    });

    it('核心聊天主链路始终在最后、作为 catch-all 兜底', async () => {
        const fired: string[] = [];
        const reg = new CommandRegistry();
        // 平台指令都不匹配 → 必须落到核心通用聊天指令。
        reg.register('lark', [recordingCmd('lark-balance', false, fired)]);
        reg.registerCore([recordingCmd('chat', true, fired)]);

        const state = await runRulesWith(msg({ channel: 'lark' }), {
            chatRules: reg.forChannel('lark'),
            botRole: undefined,
            notBlocked: async () => true,
        });

        expect(state.kind).toBe('responded');
        expect(state.matchedRule).toBe('chat');
        expect(fired).toEqual(['chat']);
    });

    it('qq 消息：不会命中 lark 注册的平台指令（归属隔离）', async () => {
        const fired: string[] = [];
        const reg = new CommandRegistry();
        reg.register('lark', [recordingCmd('lark-balance', true, fired)]);
        reg.registerCore([recordingCmd('chat', true, fired)]);

        const state = await runRulesWith(msg({ channel: 'qq' }), {
            chatRules: reg.forChannel('qq'),
            botRole: undefined,
            notBlocked: async () => true,
        });

        // qq 的 forChannel 只含核心通用指令 → 命中 chat，绝不命中 lark-balance。
        expect(state.matchedRule).toBe('chat');
        expect(fired).toEqual(['chat']);
        expect(fired).not.toContain('lark-balance');
    });
});
