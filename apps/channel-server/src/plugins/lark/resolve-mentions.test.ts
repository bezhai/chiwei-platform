import { describe, expect, it, mock } from 'bun:test';

const membersByChat = new Map<string, Array<{ user_id: string; name: string }>>();

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
        membersByChat.set('oc_mentions_1', [
            { user_id: 'on_alice', name: 'Alice' },
            { user_id: 'on_alice_wang', name: 'Alice Wang' },
        ]);

        const out = await resolveLarkMentionsForGroup(
            '@Alice Wang hi, @Alice hi',
            'oc_mentions_1',
        );

        expect(out).toBe(
            '<at user_id="on_alice_wang"></at> hi, ' +
                '<at user_id="on_alice"></at> hi',
        );
    });

    it('returns original content when the group has no active members', async () => {
        membersByChat.set('oc_mentions_empty', []);

        const out = await resolveLarkMentionsForGroup('@Nobody hi', 'oc_mentions_empty');

        expect(out).toBe('@Nobody hi');
    });
});
