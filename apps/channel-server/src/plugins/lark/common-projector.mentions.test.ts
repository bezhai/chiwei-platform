import { beforeEach, describe, expect, it, mock, afterAll } from 'bun:test';
import { botDirectory } from '@inner/shared/bot';
import type { BotConfig } from '@inner/shared/entities';

const larkUsers = new Map<
    string,
    { appId: string; openId: string; unionId?: string; name: string; commonUserId?: string }
>();
const commonUsers = new Set<string>();
const registeredBots = [
    {
        bot_name: 'current-bot',
        channel: 'lark',
        common_user_id: '018f-current-bot-common',
        credentials: {
            app_id: 'cli-current-bot',
            app_secret: 'sec',
            encrypt_key: 'enc',
            verification_token: 'vt',
            robot_union_id: 'on_current_bot',
        },
    },
    {
        bot_name: 'other-bot',
        channel: 'lark',
        common_user_id: '018f-other-bot-common',
        credentials: {
            app_id: 'cli-other-bot',
            app_secret: 'sec',
            encrypt_key: 'enc',
            verification_token: 'vt',
            robot_union_id: 'on_other_bot',
        },
    },
];

// bun 的 mock.module 是**整模块替换 + 进程级全局**，且 mock.restore() 不撤销它。
// 所以每个被桩掉的模块都先抓真身、只覆盖需要的导出，afterAll 再把真身注回去，
// 否则同一个 bun test 进程里后面加载的文件（含被测生产代码）会看到残缺模块。
// ormconfig 只导出 default（TypeORM DataSource）；import 真身只做 bindDataSource
// 存引用，不建连接。
const realOrmconfig = { ...(await import('ormconfig')) };
mock.module('ormconfig', () => ({
    ...realOrmconfig,
    default: {
        getRepository: (entity: { name?: string }) => {
            if (entity.name === 'LarkUserOpenId') {
                return {
                    findOne: mock(
                        async ({
                            where,
                        }: {
                            where: { appId: string; openId: string } | { unionId: string };
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
                        async ({ where }: { where: { appId: string; openId: string } }) => {
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
                        async (row: {
                            appId: string;
                            openId: string;
                            unionId?: string;
                            name: string;
                            commonUserId: string;
                        }) => {
                            larkUsers.set(`${row.appId}:${row.openId}`, row);
                        },
                    ),
                };
            }
            if (entity.name === 'CommonUser') {
                return {
                    upsert: mock(async (row: { common_user_id: string }) => {
                        commonUsers.add(row.common_user_id);
                    }),
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

// Redis 收敛成 @inner/shared/cache 的 RedisClient 单例后，这里桩的是
// getRedisClient()。bun 的 mock.module 是**整模块替换 + 进程级全局**，只写
// getRedisClient 会把同模块的 cache / RedisClient 等导出一并抹掉，
// 别的测试文件跟着遭殃；所以先抓真身、只覆盖这一个导出，afterAll 再注回去。
const realSharedCache = { ...(await import('@inner/shared/cache')) };
const redisStub = {
    hgetall: mock(async () => ({})),
    setNx: mock(async () => 'OK'),
    evalScript: mock(async () => 1),
    exists: mock(async () => 0),
};
mock.module('@inner/shared/cache', () => ({
    ...realSharedCache,
    getRedisClient: () => redisStub,
}));

// botDirectory 是个真实单例，被测代码只读 getAllBotConfigs()。整模块 mock 会把
// BotDirectory 类等导出一并抹掉污染别的文件，所以这里只临时改单例上的这一个方法，
// afterAll 还原。
const originalGetAllBotConfigs = botDirectory.getAllBotConfigs;
botDirectory.getAllBotConfigs = (() =>
    registeredBots as unknown as BotConfig[]) as typeof botDirectory.getAllBotConfigs;

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

afterAll(() => {
    mock.module('@inner/shared/cache', () => realSharedCache);
    mock.module('ormconfig', () => realOrmconfig);
    botDirectory.getAllBotConfigs = originalGetAllBotConfigs;
});
