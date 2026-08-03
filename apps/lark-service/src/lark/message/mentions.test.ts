import { describe, expect, it } from 'bun:test';

import { resolveLarkMentions, type LarkBotIdentity, type LarkBotLookup } from './mentions';
import type { LarkMention } from './wire';

function lookup(bots: {
    byAppId?: Record<string, LarkBotIdentity>;
    byUnionId?: Record<string, LarkBotIdentity>;
} = {}): LarkBotLookup & { appIdCalls: string[]; unionIdCalls: string[] } {
    const appIdCalls: string[] = [];
    const unionIdCalls: string[] = [];
    return {
        appIdCalls,
        unionIdCalls,
        byAppId: (appId) => {
            appIdCalls.push(appId);
            return bots.byAppId?.[appId] ?? null;
        },
        byUnionId: (unionId) => {
            unionIdCalls.push(unionId);
            return bots.byUnionId?.[unionId] ?? null;
        },
    };
}

function mention(overrides: Partial<LarkMention> = {}): LarkMention {
    return {
        key: '@_user_1',
        id: { union_id: 'on_human' },
        name: '张三',
        ...overrides,
    };
}

const persona: LarkBotIdentity = {
    botName: 'chiwei',
    displayName: '赤尾',
    commonUserId: 'cu_chiwei',
};

describe('resolveLarkMentions', () => {
    it('names a human by the name Lark sent', () => {
        const index = resolveLarkMentions([mention()], lookup());
        expect(index.all).toEqual([
            {
                token: '@_user_1',
                unionId: 'on_human',
                displayName: '张三',
                botCommonUserId: undefined,
            },
        ]);
    });

    // 人不是机器人，没必要问 bot 目录 —— 每条群消息都会走这里，多余的遍历是白花的。
    it('does not consult the bot directory for a human mention', () => {
        const bots = lookup();
        resolveLarkMentions([mention()], bots);
        expect(bots.appIdCalls).toEqual([]);
        expect(bots.unionIdCalls).toEqual([]);
    });

    it('names a registered bot by its persona, not by the raw Lark name', () => {
        const index = resolveLarkMentions(
            [
                mention({
                    mentioned_type: 'bot',
                    bot_info: { app_id: 'cli_a' },
                    id: { union_id: 'on_bot' },
                    name: 'chiwei-bot',
                }),
            ],
            lookup({ byAppId: { cli_a: persona } }),
        );
        expect(index.all[0]!.displayName).toBe('赤尾');
        expect(index.all[0]!.botCommonUserId).toBe('cu_chiwei');
    });

    it('finds a bot by union id when the event carries no app id', () => {
        const bots = lookup({ byUnionId: { on_bot: persona } });
        const index = resolveLarkMentions(
            [mention({ mentioned_type: 'bot', id: { union_id: 'on_bot' }, name: 'raw' })],
            bots,
        );
        expect(bots.unionIdCalls).toEqual(['on_bot']);
        expect(index.all[0]!.displayName).toBe('赤尾');
    });

    it('keeps the Lark name for a bot we do not run', () => {
        const index = resolveLarkMentions(
            [mention({ mentioned_type: 'bot', bot_info: { app_id: 'cli_other' }, name: '别人家的' })],
            lookup(),
        );
        expect(index.all[0]!.displayName).toBe('别人家的');
        expect(index.all[0]!.botCommonUserId).toBeUndefined();
    });

    it('keeps the Lark name for one of our bots that has no persona yet', () => {
        const index = resolveLarkMentions(
            [mention({ mentioned_type: 'bot', bot_info: { app_id: 'cli_a' }, name: 'raw-name' })],
            lookup({
                byAppId: { cli_a: { botName: 'utility', displayName: null, commonUserId: 'cu_u' } },
            }),
        );
        expect(index.all[0]!.displayName).toBe('raw-name');
        expect(index.all[0]!.botCommonUserId).toBe('cu_u');
    });

    // 名字缺失时退到 id 而不是留空：空名字会渲染成一个孤零零的 "@"，读的人根本
    // 不知道被 @ 的是谁。
    it('falls back through the id space when Lark sends no usable name', () => {
        const bots = lookup();
        expect(resolveLarkMentions([mention({ name: '   ' })], bots).all[0]!.displayName).toBe(
            'on_human',
        );
        expect(
            resolveLarkMentions([mention({ name: '', id: { user_id: 'u_1' } })], bots).all[0]!
                .displayName,
        ).toBe('u_1');
        expect(
            resolveLarkMentions([mention({ name: '', id: { open_id: 'ou_1' } })], bots).all[0]!
                .displayName,
        ).toBe('ou_1');
        expect(
            resolveLarkMentions([mention({ name: '', id: {} })], bots).all[0]!.displayName,
        ).toBe('@_user_1');
    });

    it('trims the name Lark sent', () => {
        expect(resolveLarkMentions([mention({ name: '  张三  ' })], lookup()).all[0]!.displayName).toBe(
            '张三',
        );
    });

    // 我们自己的 bot 没有 common_user_id 就意味着身份初始化没跑完。让它继续往下
    // 走，投影会拿不到 bot 身份、写出一条认不出说话人的记录 —— 必须在这里炸。
    it('refuses to resolve one of our bots before its identity is initialized', () => {
        expect(() =>
            resolveLarkMentions(
                [mention({ mentioned_type: 'bot', bot_info: { app_id: 'cli_a' } })],
                lookup({ byAppId: { cli_a: { botName: 'chiwei', displayName: '赤尾' } } }),
            ),
        ).toThrow(/chiwei/);
    });

    it('keeps every mention Lark sent, in order', () => {
        const index = resolveLarkMentions(
            [
                mention({ key: '@_user_1', name: 'A' }),
                mention({ key: '@_user_2', name: 'B' }),
                mention({ key: '@_user_3', name: 'C' }),
            ],
            lookup(),
        );
        expect(index.all.map((m) => m.displayName)).toEqual(['A', 'B', 'C']);
    });

    // 正文里的占位符就是 mention 记录的 key。按 key 查是飞书自己的约定；按数组
    // 下标查只是"key 恰好按顺序"这个巧合成立时才对。
    it('looks a mention up by the token that appears in the text', () => {
        const index = resolveLarkMentions(
            [mention({ key: '@_user_2', name: 'B' }), mention({ key: '@_user_1', name: 'A' })],
            lookup(),
        );
        expect(index.byToken('@_user_1')!.displayName).toBe('A');
        expect(index.byToken('@_user_2')!.displayName).toBe('B');
        expect(index.byToken('@_user_9')).toBeUndefined();
    });

    it('resolves an empty mention list to an empty index', () => {
        const index = resolveLarkMentions([], lookup());
        expect(index.all).toEqual([]);
        expect(index.byToken('@_user_1')).toBeUndefined();
    });
});
