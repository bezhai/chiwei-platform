import { beforeEach, describe, expect, it, mock } from 'bun:test';

const larkUsers = new Map<
    string,
    { appId: string; openId: string; unionId?: string; name: string; commonUserId?: string }
>();
const commonUsers = new Set<string>();

mock.module('ormconfig', () => ({
    default: {
        getRepository: (entity: { name?: string }) => {
            if (entity.name === 'LarkUserOpenId') {
                return {
                    findOne: mock(
                        async ({
                            where,
                        }: {
                            where:
                                | { appId: string; openId: string }
                                | { unionId: string };
                        }) => {
                            if ('unionId' in where) {
                                return (
                                    [...larkUsers.values()]
                                        .filter((row) => row.unionId === where.unionId)
                                        .sort((a, b) =>
                                            (a.commonUserId ?? '').localeCompare(
                                                b.commonUserId ?? '',
                                            ),
                                        )[0] ?? null
                                );
                            }
                            return larkUsers.get(`${where.appId}:${where.openId}`) ?? null;
                        },
                    ),
                    findOneOrFail: mock(
                        async ({
                            where,
                        }: {
                            where: { appId: string; openId: string };
                        }) => {
                            const row = larkUsers.get(`${where.appId}:${where.openId}`);
                            if (!row) throw new Error('not found');
                            return row;
                        },
                    ),
                    update: mock(
                        async (
                            where: { appId: string; openId: string },
                            patch: { unionId?: string; name?: string },
                        ) => {
                            const key = `${where.appId}:${where.openId}`;
                            const row = larkUsers.get(key);
                            if (row) larkUsers.set(key, { ...row, ...patch });
                        },
                    ),
                    upsert: mock(
                        async (
                            row: {
                                appId: string;
                                openId: string;
                                unionId?: string;
                                name: string;
                                commonUserId: string;
                            },
                        ) => {
                            larkUsers.set(`${row.appId}:${row.openId}`, row);
                        },
                    ),
                };
            }
            if (entity.name === 'CommonUser') {
                return {
                    upsert: mock(
                        async (row: { common_user_id: string }) => {
                            commonUsers.add(row.common_user_id);
                        },
                    ),
                };
            }
            return {
                findOne: mock(async () => null),
                findOneOrFail: mock(async () => ({})),
                update: mock(async () => undefined),
                upsert: mock(async () => undefined),
            };
        },
    },
}));

mock.module('@cache/redis-client', () => ({
    hgetall: mock(async () => ({})),
    setNx: mock(async () => 'OK'),
    evalScript: mock(async () => 1),
    exists: mock(async () => 0),
}));

mock.module('@integrations/rabbitmq', () => ({
    VECTORIZE: 'vectorize',
    CHAT_REQUEST: 'chat_request',
    PROACTIVE_EVAL: 'proactive_eval',
    getLane: () => undefined,
    getRabbitChannel: () => ({
        assertQueue: mock(async () => undefined),
        sendToQueue: mock(() => true),
    }),
    rabbitmqClient: { publish: mock(async () => undefined) },
}));

mock.module('@core/services/bot/multi-bot-manager', () => ({
    multiBotManager: {
        getBotConfigByAppId: (appId: string) => {
            const map: Record<string, { bot_name: string; common_user_id: string }> = {
                'cli-other-bot': {
                    bot_name: 'other-bot',
                    common_user_id: '018f-other-bot-common',
                },
            };
            return map[appId] ?? null;
        },
        getBotConfigByUnionId: (unionId: string) => {
            const map: Record<string, { bot_name: string; common_user_id: string }> = {
                on_current_bot: {
                    bot_name: 'current-bot',
                    common_user_id: '018f-current-bot-common',
                },
            };
            return map[unionId] ?? null;
        },
    },
}));

const { projectLarkMentionedCommonUserIds } = await import('./common-projector');

describe('projectLarkMentionedCommonUserIds', () => {
    beforeEach(() => {
        larkUsers.clear();
        commonUsers.clear();
    });

    it('maps current/other registered bot mentions and normal user mentions to common user ids', async () => {
        const ids = await projectLarkMentionedCommonUserIds('cli-current', [
            {
                key: '@_user_1',
                id: { union_id: 'on_current_bot', open_id: 'ou_current_bot' },
                name: 'current-bot',
                mentioned_type: 'bot',
            },
            {
                key: '@_user_2',
                id: { union_id: 'on_other_bot', open_id: 'ou_other_bot' },
                name: 'other-bot',
                mentioned_type: 'bot',
                bot_info: { app_id: 'cli-other-bot' },
            },
            {
                key: '@_user_3',
                id: { union_id: 'on_alice', open_id: 'ou_alice' },
                name: 'Alice',
                mentioned_type: 'user',
            },
        ]);

        expect(ids[0]).toBe('018f-current-bot-common');
        expect(ids[1]).toBe('018f-other-bot-common');
        expect(ids[2]).toBeDefined();
        expect(ids[2]).not.toBe('on_alice');
        expect(larkUsers.get(`cli-current:ou_alice`)?.commonUserId).toBe(ids[2]);
        expect(commonUsers.has(ids[2]!)).toBe(true);
    });

    it('deduplicates mentions after common user projection', async () => {
        const ids = await projectLarkMentionedCommonUserIds('cli-current', [
            {
                key: '@_user_1',
                id: { union_id: 'on_current_bot', open_id: 'ou_current_bot' },
                name: 'current-bot',
            },
            {
                key: '@_user_2',
                id: { union_id: 'on_current_bot', open_id: 'ou_current_bot' },
                name: 'current-bot',
            },
        ]);

        expect(ids).toEqual(['018f-current-bot-common']);
    });

    it('reuses an existing common user across app-scoped open ids when union id matches', async () => {
        larkUsers.set('cli-a:ou_alice_a', {
            appId: 'cli-a',
            openId: 'ou_alice_a',
            unionId: 'on_alice',
            name: 'Alice',
            commonUserId: '018f-alice-common',
        });

        const ids = await projectLarkMentionedCommonUserIds('cli-b', [
            {
                key: '@_user_1',
                id: { union_id: 'on_alice', open_id: 'ou_alice_b' },
                name: 'Alice',
                mentioned_type: 'user',
            },
        ]);

        expect(ids).toEqual(['018f-alice-common']);
        expect(larkUsers.get('cli-b:ou_alice_b')?.commonUserId).toBe('018f-alice-common');
        expect(commonUsers.has('018f-alice-common')).toBe(true);
    });
});
