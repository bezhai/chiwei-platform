import { describe, expect, it, mock } from 'bun:test';

let currentAppId = 'cli_a';
const membersByAppChat = new Map<string, Array<{ user_id: string; name: string }>>();

function queryBuilder() {
    let chatId = '';
    let appId = '';
    return {
        innerJoin(_entity: unknown, _alias: string, _condition: string, params?: { appId?: string }) {
            if (params?.appId) {
                appId = params.appId;
            }
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
            return membersByAppChat.get(`${appId}:${chatId}`) ?? [];
        },
    };
}

mock.module('./bot-identity', () => ({
    getCurrentLarkBotAppId: () => currentAppId,
}));

mock.module('ormconfig', () => ({
    default: {
        getRepository: () => ({
            createQueryBuilder: () => queryBuilder(),
        }),
    },
}));

const { resolveLarkMentionsForGroup } = await import('./resolve-mentions');

describe('resolveLarkMentionsForGroup', () => {
    it('replaces longer names first to avoid partial @name matches', async () => {
        currentAppId = 'cli_a';
        membersByAppChat.set('cli_a:oc_mentions_1', [
            { user_id: 'ou_alice', name: 'Alice' },
            { user_id: 'ou_alice_wang', name: 'Alice Wang' },
        ]);

        const out = await resolveLarkMentionsForGroup(
            '@Alice Wang hi, @Alice hi',
            'oc_mentions_1',
        );

        expect(out).toBe(
            '<at user_id="ou_alice_wang"></at> hi, ' +
                '<at user_id="ou_alice"></at> hi',
        );
    });

    it('returns original content when the group has no active members', async () => {
        currentAppId = 'cli_a';
        membersByAppChat.set('cli_a:oc_mentions_empty', []);

        const out = await resolveLarkMentionsForGroup('@Nobody hi', 'oc_mentions_empty');

        expect(out).toBe('@Nobody hi');
    });

    it('keeps cached open ids isolated per bot app id', async () => {
        membersByAppChat.set('cli_a:oc_same_chat', [
            { user_id: 'ou_alice_for_a', name: 'Alice' },
        ]);
        membersByAppChat.set('cli_b:oc_same_chat', [
            { user_id: 'ou_alice_for_b', name: 'Alice' },
        ]);

        currentAppId = 'cli_a';
        const first = await resolveLarkMentionsForGroup('@Alice', 'oc_same_chat');
        currentAppId = 'cli_b';
        const second = await resolveLarkMentionsForGroup('@Alice', 'oc_same_chat');

        expect(first).toBe('<at user_id="ou_alice_for_a"></at>');
        expect(second).toBe('<at user_id="ou_alice_for_b"></at>');
    });
});
