import { beforeEach, describe, expect, it, mock, afterAll } from 'bun:test';

// Maps standing in for the lark_message / common_* tables.
const larkMessages = new Map<string, { om_id: string; common_message_id: string }>();
const larkUsers = new Map<string, { commonUserId: string }>();
const larkChats = new Map<string, { common_conversation_id: string }>();

const warn = mock((_message: string, _meta?: Record<string, unknown>) => undefined);

// bun 的 mock.module 是**整模块替换 + 进程级全局**，且 mock.restore() 不撤销它。
// 每个被桩掉的模块都先抓真身、只覆盖需要的导出，afterAll 再注回真身，否则同一个
// bun test 进程里后面加载的文件（含被测生产代码）会看到残缺模块。
// @logger/index 还导出 LoggerFactory，只写 default 会把它抹掉。
const realLogger = { ...(await import('@logger/index')) };
mock.module('@logger/index', () => ({
    ...realLogger,
    default: {
        warn,
        info: mock(() => undefined),
        error: mock(() => undefined),
        debug: mock(() => undefined),
    },
}));

// ormconfig 只导出 default（TypeORM DataSource）；import 真身只做 bindDataSource
// 存引用，不建连接。
const realOrmconfig = { ...(await import('ormconfig')) };
mock.module('ormconfig', () => ({
    ...realOrmconfig,
    default: {
        getRepository: (entity: { name?: string }) => {
            if (entity.name === 'LarkMessage') {
                return {
                    findOne: mock(async ({ where }: { where: { om_id?: string } }) => {
                        if (where.om_id) return larkMessages.get(where.om_id) ?? null;
                        return null;
                    }),
                };
            }
            if (entity.name === 'LarkUserOpenId') {
                return {
                    findOne: mock(
                        async ({
                            where,
                        }: {
                            where: { appId: string; openId: string } | { unionId: string };
                        }) => {
                            if ('unionId' in where) return null;
                            return larkUsers.get(`${where.appId}:${where.openId}`) ?? null;
                        },
                    ),
                    findOneOrFail: mock(
                        async ({ where }: { where: { appId: string; openId: string } }) => {
                            const row = larkUsers.get(`${where.appId}:${where.openId}`);
                            return row ?? { commonUserId: '018f-user' };
                        },
                    ),
                    upsert: mock(
                        async (row: { appId: string; openId: string; commonUserId: string }) => {
                            larkUsers.set(`${row.appId}:${row.openId}`, {
                                commonUserId: row.commonUserId,
                            });
                        },
                    ),
                };
            }
            if (entity.name === 'LarkBaseChatInfo') {
                return {
                    findOne: mock(async ({ where }: { where: { chat_id: string } }) => {
                        return larkChats.get(where.chat_id) ?? null;
                    }),
                    findOneOrFail: mock(async ({ where }: { where: { chat_id: string } }) => {
                        const row = larkChats.get(where.chat_id);
                        return row ?? { common_conversation_id: '018f-chat' };
                    }),
                    upsert: mock(
                        async (row: { chat_id: string; common_conversation_id: string }) => {
                            larkChats.set(row.chat_id, {
                                common_conversation_id: row.common_conversation_id,
                            });
                        },
                    ),
                    update: mock(async () => undefined),
                };
            }
            // CommonUser / CommonConversation: write-only sinks here.
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

// 这里不桩 @middleware/context / @inner/shared/mq / @inner/shared/bot：
// prepareLarkInboundProjection 全程不读 context（context.getBotName 只在
// storeLarkInboundMessage 用）；common-projector 的 import 图里根本没有
// @inner/shared/mq；botDirectory 也不会被碰到 —— 事件带 app_id（不走
// getCurrentLarkBotAppId）、mentions 为空（不走 bot 提及投影）。
const { prepareLarkInboundProjection } = await import('./common-projector');

// A reply message whose parent_id/root_id point at an om_id that was never
// projected into lark_message (typical in group chats where not every message
// is processed). Mirrors the real Lark "回复引用" event shape.
function replyEvent(opts: { rootId?: string; parentId?: string }) {
    return {
        app_id: 'cli-current',
        sender: {
            sender_id: {
                open_id: 'ou_sender',
                union_id: 'on_sender',
            },
        },
        message: {
            message_id: 'om_self',
            chat_id: 'oc_group',
            root_id: opts.rootId,
            parent_id: opts.parentId,
            message_type: 'text',
            create_time: '1780309200000',
            mentions: [],
        },
    } as any;
}

const groupMessage = {
    senderInfo: { name: 'sender', avatar_origin: 'avatar' },
    groupChatInfo: {
        name: 'group',
        avatar: 'group-avatar',
        user_count: 3,
        is_leave: false,
    },
    isP2P: () => false,
    allowDownloadResource: () => true,
} as any;

const inbound = {
    conversation_scope: 'group',
    content: [{ kind: 'text', text: 'reply text' }],
} as any;

describe('prepareLarkInboundProjection reply ingest', () => {
    beforeEach(() => {
        larkMessages.clear();
        larkUsers.clear();
        larkChats.clear();
        warn.mockClear();
    });

    it('tolerates a parent_id whose referenced message has no common mapping', async () => {
        const projection = await prepareLarkInboundProjection(
            replyEvent({ parentId: 'om_unknown_parent' }),
            groupMessage,
            inbound,
        );

        // Reply chain field stays empty because the parent was never projected.
        expect(projection.commonReplyMessageId).toBeUndefined();
        // This message itself still gets a valid common_message_id.
        expect(projection.commonMessageId).toBeDefined();
        expect(projection.commonMessageId.length).toBeGreaterThan(0);
    });

    it('tolerates a root_id whose referenced message has no common mapping', async () => {
        const projection = await prepareLarkInboundProjection(
            replyEvent({ rootId: 'om_unknown_root' }),
            groupMessage,
            inbound,
        );

        // Falls back to the message's own id when the root cannot be resolved.
        expect(projection.commonRootMessageId).toBe(projection.commonMessageId);
        expect(projection.commonMessageId).toBeDefined();
    });

    it('still resolves reply/root chain when the referenced message is mapped', async () => {
        larkMessages.set('om_known', {
            om_id: 'om_known',
            common_message_id: '018f-known-common',
        });

        const projection = await prepareLarkInboundProjection(
            replyEvent({ rootId: 'om_known', parentId: 'om_known' }),
            groupMessage,
            inbound,
        );

        expect(projection.commonReplyMessageId).toBe('018f-known-common');
        expect(projection.commonRootMessageId).toBe('018f-known-common');
    });

    it('warns once with om_id and parent label when a parent reference is dropped', async () => {
        await prepareLarkInboundProjection(
            replyEvent({ parentId: 'om_unknown_parent' }),
            groupMessage,
            inbound,
        );

        expect(warn).toHaveBeenCalledTimes(1);
        const [message, meta] = warn.mock.calls[0]!;
        expect(message).toContain('om_unknown_parent');
        expect(meta).toMatchObject({
            referenceKind: 'parent',
            referencedOmId: 'om_unknown_parent',
            selfOmId: 'om_self',
        });
    });

    it('warns once with om_id and root label when a root reference is dropped', async () => {
        await prepareLarkInboundProjection(
            replyEvent({ rootId: 'om_unknown_root' }),
            groupMessage,
            inbound,
        );

        expect(warn).toHaveBeenCalledTimes(1);
        const [message, meta] = warn.mock.calls[0]!;
        expect(message).toContain('om_unknown_root');
        expect(meta).toMatchObject({
            referenceKind: 'root',
            referencedOmId: 'om_unknown_root',
            selfOmId: 'om_self',
        });
    });

    it('does not warn when the referenced message resolves', async () => {
        larkMessages.set('om_known', {
            om_id: 'om_known',
            common_message_id: '018f-known-common',
        });

        await prepareLarkInboundProjection(
            replyEvent({ rootId: 'om_known', parentId: 'om_known' }),
            groupMessage,
            inbound,
        );

        expect(warn).not.toHaveBeenCalled();
    });
});

afterAll(() => {
    mock.module('@inner/shared/cache', () => realSharedCache);
    mock.module('@logger/index', () => realLogger);
    mock.module('ormconfig', () => realOrmconfig);
});
