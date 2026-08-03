import { afterEach, beforeEach, describe, expect, it, mock, afterAll } from 'bun:test';
import type { LarkInboundProjection } from './common-projector';

const larkMessages = new Map<string, { om_id: string; common_message_id: string }>();
const commonMessages = new Set<string>();
const commonMessageRows = new Map<string, Record<string, unknown>>();
const commonInsertCalls: Record<string, unknown>[] = [];
const larkInsertCalls: unknown[] = [];
let larkInsertRaceWinner: { om_id: string; common_message_id: string } | null = null;

function insertBuilder() {
    let target: { name?: string } | undefined;
    let payload: Record<string, unknown> = {};
    return {
        insert() {
            return this;
        },
        into(entity: { name?: string }) {
            target = entity;
            return this;
        },
        values(value: Record<string, unknown>) {
            payload = value;
            return this;
        },
        orIgnore() {
            return this;
        },
        async execute() {
            if (target?.name === 'CommonMessage') {
                commonInsertCalls.push(payload);
                const id = payload.common_message_id as string;
                if (commonMessages.has(id)) {
                    return { identifiers: [] };
                }
                commonMessages.add(id);
                commonMessageRows.set(id, payload);
                return { identifiers: [{ common_message_id: id }] };
            }
            if (target?.name === 'LarkMessage') {
                larkInsertCalls.push(payload);
                const omId = payload.om_id as string;
                if (larkInsertRaceWinner?.om_id === omId) {
                    larkMessages.set(omId, larkInsertRaceWinner);
                    throw new Error(
                        'duplicate key value violates unique constraint "lark_message_pkey"',
                    );
                }
                if (!larkMessages.has(omId)) {
                    larkMessages.set(omId, {
                        om_id: omId,
                        common_message_id: payload.common_message_id as string,
                    });
                }
                return { identifiers: [{ om_id: omId }] };
            }
            throw new Error(`unexpected insert target: ${target?.name}`);
        },
    };
}

// bun 的 mock.module 是**整模块替换 + 进程级全局**，且 mock.restore() 不撤销它。
// 先抓真身、只覆盖需要的导出，afterAll 再注回真身，否则同一个 bun test 进程里
// 后面加载的文件（含被测生产代码）会看到残缺模块。ormconfig 只导出 default
// （TypeORM DataSource）；import 真身只做 bindDataSource 存引用，不建连接。
const realOrmconfig = { ...(await import('ormconfig')) };
mock.module('ormconfig', () => ({
    ...realOrmconfig,
    default: {
        createEntityManager: mock(() => ({})),
        getRepository: (entity: { name?: string }) => {
            if (entity.name === 'LarkMessage') {
                return {
                    findOne: mock(
                        async ({
                            where,
                        }: {
                            where: { om_id?: string; common_message_id?: string };
                        }) => {
                            if (where.om_id) return larkMessages.get(where.om_id) ?? null;
                            return null;
                        },
                    ),
                };
            }
            if (entity.name === 'CommonMessage') {
                return {
                    update: mock(
                        async (
                            where: { common_message_id: string; role: string },
                            patch: Record<string, unknown>,
                        ) => {
                            const row = commonMessageRows.get(where.common_message_id);
                            if (!row || row.role !== where.role) return { affected: 0 };
                            commonMessageRows.set(where.common_message_id, {
                                ...row,
                                ...patch,
                            });
                            return { affected: 1 };
                        },
                    ),
                };
            }
            return {
                findOne: mock(async () => null),
                findOneOrFail: mock(async () => ({})),
                find: mock(async () => []),
                save: mock(async (value: unknown) => value),
                create: mock((value: unknown) => value),
                update: mock(async () => ({ affected: 0 })),
                upsert: mock(async () => ({ identifiers: [] })),
            };
        },
        transaction: async (task: (manager: unknown) => Promise<void>) => {
            const commonSnapshot = new Set(commonMessages);
            const commonRowSnapshot = new Map(commonMessageRows);
            const larkSnapshot = new Map(larkMessages);
            try {
                return await task({
                    getRepository: (entity: { name?: string }) => {
                        if (entity.name === 'LarkMessage') {
                            return {
                                findOne: mock(
                                    async ({ where }: { where: { om_id: string } }) =>
                                        larkMessages.get(where.om_id) ?? null,
                                ),
                            };
                        }
                        throw new Error(`unexpected repository: ${entity.name}`);
                    },
                    createQueryBuilder: () => insertBuilder(),
                });
            } catch (err) {
                commonMessages.clear();
                for (const id of commonSnapshot) commonMessages.add(id);
                commonMessageRows.clear();
                for (const [id, row] of commonRowSnapshot) commonMessageRows.set(id, row);
                larkMessages.clear();
                for (const [omId, row] of larkSnapshot) larkMessages.set(omId, row);
                throw err;
            }
        },
    },
}));

// Redis 收敛成 @inner/shared/cache 的 RedisClient 单例后，这里桩的是
// getRedisClient()。bun 的 mock.module 是**整模块替换 + 进程级全局**，只写
// getRedisClient 会把同模块的 cache / RedisClient 等导出一并抹掉，
// 别的测试文件跟着遭殃；所以先抓真身、只覆盖这一个导出，afterAll 再注回去。
const realSharedCache = { ...(await import('@inner/shared/cache')) };
const redisStub = {
    get: mock(async () => null),
    setWithExpire: mock(async () => 'OK'),
    hgetall: mock(async () => ({})),
    setNx: mock(async () => 'OK'),
    evalScript: mock(async () => 1),
    exists: mock(async () => 0),
};
mock.module('@inner/shared/cache', () => ({
    ...realSharedCache,
    getRedisClient: () => redisStub,
}));
// 这里不桩 @middleware/context / @inner/shared/mq：storeLarkInboundMessage 走的是
// `context.getBotName() || 'chiwei'`，无请求上下文时真 context 返回空串、结果与
// 桩一致（用例也不断言 bot_name）；common-projector 的 import 图里根本没有
// @inner/shared/mq。
import { botDirectory } from '@inner/shared/bot';

const {
    claimLarkInboundMessageForBot,
    storeLarkInboundMessage,
    storeLarkOutboundMessage,
} = await import('./common-projector');
const originalGetBotCommonUserId = botDirectory.getBotCommonUserId;

afterEach(() => {
    botDirectory.getBotCommonUserId = originalGetBotCommonUserId;
});

function event(omId: string) {
    return {
        sender: {
            sender_id: {
                open_id: 'ou_sender',
                union_id: 'on_sender',
            },
        },
        message: {
            message_id: omId,
            chat_id: 'oc_chat',
            root_id: undefined,
            parent_id: undefined,
            message_type: 'text',
            create_time: '1780309200000',
        },
    };
}

const message = {
    senderInfo: { name: 'sender' },
} as any;

function projection(commonMessageId: string): LarkInboundProjection {
    return {
        commonUserId: '018f-user',
        commonConversationId: '018f-chat',
        commonMessageId,
        commonRootMessageId: commonMessageId,
        commonReplyMessageId: undefined,
        mentionedUserIds: [],
        content: [{ kind: 'text', text: 'hello' }],
        contentText: 'hello',
        scope: 'group',
    };
}

describe('storeLarkInboundMessage', () => {
    beforeEach(() => {
        larkMessages.clear();
        commonMessages.clear();
        commonMessageRows.clear();
        commonInsertCalls.length = 0;
        larkInsertCalls.length = 0;
        larkInsertRaceWinner = null;
    });

    it('writes the lark mapping once for a new om_id', async () => {
        await storeLarkInboundMessage(event('om_1') as any, projection('018f-common-1'), message);

        expect(larkMessages.get('om_1')?.common_message_id).toBe('018f-common-1');
        expect(larkInsertCalls.length).toBe(1);
    });

    it('skips lark_message insert when the om_id already maps to the same common id', async () => {
        commonMessages.add('018f-common-1');
        larkMessages.set('om_1', {
            om_id: 'om_1',
            common_message_id: '018f-common-1',
        });

        await storeLarkInboundMessage(event('om_1') as any, projection('018f-common-1'), message);

        expect(larkInsertCalls.length).toBe(0);
    });

    it('fails loud when an om_id is already mapped to another common id', async () => {
        larkMessages.set('om_1', {
            om_id: 'om_1',
            common_message_id: '018f-common-existing',
        });

        await expect(
            storeLarkInboundMessage(event('om_1') as any, projection('018f-common-new'), message),
        ).rejects.toThrow(/already maps to 018f-common-existing/);
    });

    it('rolls back common_message when lark_message insert loses a race', async () => {
        larkInsertRaceWinner = {
            om_id: 'om_1',
            common_message_id: '018f-common-existing',
        };

        await expect(
            storeLarkInboundMessage(event('om_1') as any, projection('018f-common-new'), message),
        ).rejects.toThrow(/mapping insert failed/);

        expect(commonMessages.has('018f-common-new')).toBe(false);
        expect(larkMessages.has('om_1')).toBe(false);
    });

    it('lets the responding bot claim an existing user common_message', async () => {
        await storeLarkInboundMessage(event('om_1') as any, projection('018f-common-1'), message);

        await claimLarkInboundMessageForBot({
            commonMessageId: '018f-common-1',
            botName: 'dev',
            commonUserId: '018f-canonical-user',
        });

        expect(commonMessageRows.get('018f-common-1')?.bot_name).toBe('dev');
        expect(commonMessageRows.get('018f-common-1')?.common_user_id).toBe(
            '018f-canonical-user',
        );
    });
});

describe('storeLarkOutboundMessage', () => {
    beforeEach(() => {
        larkMessages.clear();
        commonMessages.clear();
        commonMessageRows.clear();
        commonInsertCalls.length = 0;
        larkInsertCalls.length = 0;
        botDirectory.getBotCommonUserId = (() =>
            '018f-bot-common-user') as typeof botDirectory.getBotCommonUserId;
    });

    it('writes assistant common_message with the bot common user id', async () => {
        await storeLarkOutboundMessage({
            omId: 'om_assistant_1',
            chatId: 'oc_chat',
            commonConversationId: '018f-chat',
            commonRootMessageId: undefined,
            commonReplyMessageId: undefined,
            contentText: 'reply',
            botName: 'chiwei',
            senderDisplayName: '赤尾',
            scope: 'group',
            eventTime: 1780309200000,
            messageType: 'text',
            responseId: 'resp-1',
        });

        expect(commonInsertCalls[0].role).toBe('assistant');
        expect(commonInsertCalls[0].common_user_id).toBe('018f-bot-common-user');
    });
});

afterAll(() => {
    mock.module('@inner/shared/cache', () => realSharedCache);
    mock.module('ormconfig', () => realOrmconfig);
});
