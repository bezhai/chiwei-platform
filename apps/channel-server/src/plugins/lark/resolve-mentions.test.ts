import { afterAll, describe, expect, it, mock } from 'bun:test';

const membersByChat = new Map<string, Array<{ union_id: string; name: string }>>();
let botAliases: Array<{ union_id: string; name: string }> = [];

function queryBuilder() {
    let chatId = '';
    return {
        innerJoin() {
            return this;
        },
        select() {
            return this;
        },
        where(_sql: string, params: { chatId: string }) {
            chatId = params.chatId;
            return this;
        },
        andWhere() {
            return this;
        },
        async getRawMany() {
            return membersByChat.get(chatId) ?? [];
        },
    };
}

// mock.module 是进程级整模块替换、mock.restore() 撤不掉它。两个模块都要先抓
// 真身铺底（bot-identity 在 ormconfig 被替换前抓，拿到的是真 DataSource），
// 只覆盖本文件真正要控的导出，跑完再原样装回去。
const realOrmconfig = { ...(await import('ormconfig')) };
const realBotIdentity = { ...(await import('./bot-identity')) };

mock.module('ormconfig', () => ({
    ...realOrmconfig,
    default: {
        getRepository: () => ({
            createQueryBuilder: () => queryBuilder(),
        }),
    },
}));

mock.module('./bot-identity', () => ({
    ...realBotIdentity,
    getLarkBotMentionAliases: () => botAliases,
}));

afterAll(() => {
    mock.module('ormconfig', () => realOrmconfig);
    mock.module('./bot-identity', () => realBotIdentity);
});

const { resolveLarkMentionsForGroup } = await import('./resolve-mentions');

describe('resolveLarkMentionsForGroup', () => {
    it('replaces longer names first to avoid partial @name matches', async () => {
        membersByChat.set('oc_mentions_1', [
            { union_id: 'ou_alice', name: 'Alice' },
            { union_id: 'ou_alice_wang', name: 'Alice Wang' },
        ]);

        const out = await resolveLarkMentionsForGroup(
            '@Alice Wang hi, @Alice hi',
            'oc_mentions_1',
        );

        expect(out).toBe(
            '<at user_id="ou_alice_wang">Alice Wang</at> hi, ' +
                '<at user_id="ou_alice">Alice</at> hi',
        );
    });

    it('returns original content when the group has no active members', async () => {
        membersByChat.set('oc_mentions_empty', []);
        botAliases = [];

        const out = await resolveLarkMentionsForGroup('@Nobody hi', 'oc_mentions_empty');

        expect(out).toBe('@Nobody hi');
    });

    it('resolves registered bot persona display names as group mention aliases', async () => {
        membersByChat.set('oc_bot_alias', [
            { union_id: 'on_ayana', name: '天才小画家绫奈' },
        ]);
        botAliases = [{ union_id: 'on_ayana', name: '绫奈' }];

        const out = await resolveLarkMentionsForGroup(
            '@绫奈在吗 @天才小画家绫奈也在吗',
            'oc_bot_alias',
        );

        expect(out).toBe(
            '<at user_id="on_ayana">绫奈</at>在吗 ' +
                '<at user_id="on_ayana">天才小画家绫奈</at>也在吗',
        );
    });
});
