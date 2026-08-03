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

    // 目录是唯一的判据：一个 mention 是不是我们的 bot，只有问过才知道。
    it('asks the directory about every mention, including one that looks human', () => {
        const bots = lookup();
        const index = resolveLarkMentions([mention()], bots);
        expect(bots.unionIdCalls).toEqual(['on_human']);
        expect(index.all[0]!.botCommonUserId).toBeUndefined();
    });

    // 飞书把我们自己 bot 的 mention 标成 user（或者老格式事件根本不带这个字段）
    // 时，认不出来的后果是双份的：botCommonUserId 为空 → NeedRobotMention 判定
    // "没被 @" → 群里 @ 赤尾不回复；同时这个 bot 会掉进真人分支，被铸出一个"真人"
    // common_user。所以判据只能是 app_id / union_id 命中目录，不能再加条件。
    it('identifies one of our bots even when Lark labels the mention as a user', () => {
        const index = resolveLarkMentions(
            [
                mention({
                    mentioned_type: 'user',
                    bot_info: { app_id: 'cli_a' },
                    id: { union_id: 'on_bot' },
                    name: 'chiwei-raw',
                }),
            ],
            lookup({ byAppId: { cli_a: persona } }),
        );
        expect(index.all[0]!.displayName).toBe('赤尾');
        expect(index.all[0]!.botCommonUserId).toBe('cu_chiwei');
    });

    it('identifies one of our bots when the event carries no mentioned_type at all', () => {
        const index = resolveLarkMentions(
            [mention({ id: { union_id: 'on_bot' }, name: 'chiwei-raw' })],
            lookup({ byUnionId: { on_bot: persona } }),
        );
        expect(index.all[0]!.displayName).toBe('赤尾');
        expect(index.all[0]!.botCommonUserId).toBe('cu_chiwei');
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

    // app_id 认不出来**不等于**不是我们的 bot：新 bot 刚上线时目录里可能还没有它的
    // app_id、但 union_id 已经在了。在这里短路的后果跟上面那条一样 —— 群里 @ 不回复，
    // 外加给这个 bot 铸一个"真人"common_user。两个都问过才能说"不是我们的"。
    it('falls back to the union id when the app id is not in the directory', () => {
        const bots = lookup({ byUnionId: { on_bot: persona } });
        const index = resolveLarkMentions(
            [
                mention({
                    mentioned_type: 'bot',
                    bot_info: { app_id: 'cli_not_yet_known' },
                    id: { union_id: 'on_bot' },
                    name: 'chiwei-raw',
                }),
            ],
            bots,
        );
        expect(bots.appIdCalls).toEqual(['cli_not_yet_known']);
        expect(bots.unionIdCalls).toEqual(['on_bot']);
        expect(index.all[0]!.displayName).toBe('赤尾');
        expect(index.all[0]!.botCommonUserId).toBe('cu_chiwei');
    });

    // 反过来：app_id 就能认出来时不必再问一遍 union_id。
    it('stops at the app id when that already identifies one of our bots', () => {
        const bots = lookup({ byAppId: { cli_a: persona } });
        resolveLarkMentions(
            [mention({ mentioned_type: 'bot', bot_info: { app_id: 'cli_a' }, id: { union_id: 'on_bot' } })],
            bots,
        );
        expect(bots.unionIdCalls).toEqual([]);
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
