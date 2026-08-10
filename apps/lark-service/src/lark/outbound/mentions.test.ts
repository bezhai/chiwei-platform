import { describe, expect, it } from 'bun:test';
import type { BotConfig } from '@inner/shared/entities';

import type { LarkBotRoster } from '../bot-lookup';
import {
    createLarkMentionResolver,
    larkBotAliases,
    withRosterCache,
    type LarkGroupRoster,
    type LarkMentionTarget,
    type LarkRosterEntry,
} from './mentions';

function member(
    unionId: string,
    name: string,
    hasLeft = false,
): LarkRosterEntry {
    return { unionId, name, hasLeft };
}

function roster(byChat: Record<string, LarkRosterEntry[]>): LarkGroupRoster & { calls: string[] } {
    const calls: string[] = [];
    return {
        calls,
        async entries(chatId) {
            calls.push(chatId);
            return byChat[chatId] ?? [];
        },
    };
}

function resolver(entries: LarkRosterEntry[], aliases: LarkMentionTarget[] = []) {
    return createLarkMentionResolver({
        roster: roster({ oc_1: entries }),
        aliases: () => aliases,
    });
}

describe('createLarkMentionResolver', () => {
    it('把 @名字 换成飞书的 at 标签', async () => {
        const resolve = resolver([member('on_alice', 'Alice')]);
        expect(await resolve('喂 @Alice 在吗', 'oc_1')).toBe(
            '喂 <at user_id="on_alice">Alice</at> 在吗',
        );
    });

    it('先长名后短名，短名不吃掉长名的前缀', async () => {
        // 反过来的话 "@Alice Wang" 会先被 "Alice" 吃掉一半，变成
        // "<at ...>Alice</at> Wang" —— @ 错人，而且看不出来。
        const resolve = resolver([
            member('on_alice', 'Alice'),
            member('on_alice_wang', 'Alice Wang'),
        ]);

        expect(await resolve('@Alice Wang hi, @Alice hi', 'oc_1')).toBe(
            '<at user_id="on_alice_wang">Alice Wang</at> hi, ' +
                '<at user_id="on_alice">Alice</at> hi',
        );
    });

    it('已经退群的人不参与匹配', async () => {
        // 退群的人还留在花名册里（历史消息要查得到他叫什么），但 @ 一个已经不在群里
        // 的人飞书那边渲染成一个死链接，而且他也收不到。
        const resolve = resolver([member('on_gone', '离职的老王', true)]);
        expect(await resolve('@离职的老王 你在吗', 'oc_1')).toBe('@离职的老王 你在吗');
    });

    it('自己人的别名也认，同一个人可以有多个叫法', async () => {
        const resolve = resolver(
            [member('on_ayana', '天才小画家绫奈')],
            [{ unionId: 'on_ayana', name: '绫奈' }],
        );

        expect(await resolve('@绫奈在吗 @天才小画家绫奈也在吗', 'oc_1')).toBe(
            '<at user_id="on_ayana">绫奈</at>在吗 ' +
                '<at user_id="on_ayana">天才小画家绫奈</at>也在吗',
        );
    });

    it('花名册和别名给出同一个人同一个叫法时只算一次', async () => {
        const resolve = resolver(
            [member('on_ayana', '绫奈')],
            [{ unionId: 'on_ayana', name: '绫奈' }],
        );

        expect(await resolve('@绫奈', 'oc_1')).toBe('<at user_id="on_ayana">绫奈</at>');
    });

    it('没有名字的成员跳过，不产出一个光秃秃的 @', async () => {
        const resolve = resolver([member('on_noname', ''), member('on_alice', 'Alice')]);
        expect(await resolve('@Alice 和 @', 'oc_1')).toBe(
            '<at user_id="on_alice">Alice</at> 和 @',
        );
    });

    it('群里一个人都没有时原样返回', async () => {
        const resolve = resolver([]);
        expect(await resolve('@谁都不是 hi', 'oc_1')).toBe('@谁都不是 hi');
    });

    it('名字里带正则替换的特殊记号（$&）也照原样写进标签', async () => {
        // replaceAll 的替换串里 `$&` 是"整个匹配"的占位符。当成普通字符串拼进去
        // 会把名字撑成一坨乱码。
        const resolve = resolver([member('on_weird', 'A$&B')]);
        expect(await resolve('@A$&B hi', 'oc_1')).toBe('<at user_id="on_weird">A$&B</at> hi');
    });
});

describe('withRosterCache', () => {
    it('同一个群在有效期内只查一次', async () => {
        const base = roster({ oc_1: [member('on_a', 'A')] });
        let now = 1_000;
        const cached = withRosterCache(base, 60_000, () => now);

        await cached.entries('oc_1');
        now = 30_000;
        await cached.entries('oc_1');

        expect(base.calls).toEqual(['oc_1']);
    });

    it('过期之后重新查', async () => {
        const base = roster({ oc_1: [member('on_a', 'A')] });
        let now = 1_000;
        const cached = withRosterCache(base, 60_000, () => now);

        await cached.entries('oc_1');
        now = 61_001;
        await cached.entries('oc_1');

        expect(base.calls).toEqual(['oc_1', 'oc_1']);
    });

    it('每个群各缓存各的', async () => {
        const base = roster({ oc_1: [member('on_a', 'A')], oc_2: [member('on_b', 'B')] });
        const cached = withRosterCache(base, 60_000, () => 1_000);

        expect(await cached.entries('oc_1')).toEqual([member('on_a', 'A')]);
        expect(await cached.entries('oc_2')).toEqual([member('on_b', 'B')]);
        expect(base.calls).toEqual(['oc_1', 'oc_2']);
    });
});

function bot(overrides: Partial<BotConfig> = {}): BotConfig {
    return {
        bot_name: 'chiwei',
        channel: 'lark',
        persona_id: 'p_chiwei',
        credentials: {
            app_id: 'cli_chiwei',
            app_secret: 's',
            encrypt_key: 'e',
            verification_token: 'v',
            robot_union_id: 'on_chiwei',
        },
        ...overrides,
    } as BotConfig;
}

const botRoster = (bots: BotConfig[]): LarkBotRoster => ({ getAllBotConfigs: () => bots });

describe('larkBotAliases', () => {
    it('用人设名当群里的叫法，union id 取 bot 自己的', async () => {
        expect(larkBotAliases(botRoster([bot()]), () => '赤尾')).toEqual([
            { unionId: 'on_chiwei', name: '赤尾' },
        ]);
    });

    it('没绑人设的工具 bot 不进别名表', async () => {
        expect(larkBotAliases(botRoster([bot({ persona_id: undefined })]), () => '赤尾')).toEqual(
            [],
        );
    });

    it('人设查不到名字的也不进', async () => {
        expect(larkBotAliases(botRoster([bot()]), () => null)).toEqual([]);
    });

    it('别的渠道的 bot 一概不看', async () => {
        // 问一个 QQ bot 要飞书凭据会抛。别名表是每条群聊出站都要走的路，不该因为
        // 目录里混进一条别的渠道的记录就整条链路炸掉。
        expect(
            larkBotAliases(
                botRoster([bot({ channel: 'qq', credentials: { open_id: 'x' } })]),
                () => '赤尾',
            ),
        ).toEqual([]);
    });
});
