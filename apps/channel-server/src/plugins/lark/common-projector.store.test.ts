import { beforeEach, describe, expect, it, mock } from 'bun:test';

const larkMessages = new Map<string, { om_id: string; common_message_id: string }>();
const commonMessages = new Set<string>();
const larkInsertCalls: unknown[] = [];
const vectorizePublishes: unknown[] = [];
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
                const id = payload.common_message_id as string;
                if (commonMessages.has(id)) {
                    return { identifiers: [] };
                }
                commonMessages.add(id);
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

mock.module('ormconfig', () => ({
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
                larkMessages.clear();
                for (const [omId, row] of larkSnapshot) larkMessages.set(omId, row);
                throw err;
            }
        },
    },
}));

mock.module('@cache/redis-client', () => ({
    get: mock(async () => null),
    setWithExpire: mock(async () => 'OK'),
    hgetall: mock(async () => ({})),
    setNx: mock(async () => 'OK'),
    evalScript: mock(async () => 1),
    exists: mock(async () => 0),
}));
mock.module('@middleware/context', () => ({
    context: {
        getBotName: () => 'chiwei',
        getLane: () => undefined,
    },
}));
mock.module('@integrations/rabbitmq', () => ({
    VECTORIZE: 'vectorize',
    PROACTIVE_EVAL: 'proactive_eval',
    CHAT_REQUEST: 'chat_request',
    getLane: () => undefined,
    getRabbitChannel: () => ({
        assertQueue: mock(async () => undefined),
        sendToQueue: mock(() => true),
    }),
    rabbitmqClient: {
        publish: mock(async (...args: unknown[]) => {
            vectorizePublishes.push(args);
        }),
    },
}));
const { storeLarkInboundMessage } = await import('./common-projector');

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

function projection(commonMessageId: string) {
    return {
        commonUserId: '018f-user',
        commonConversationId: '018f-chat',
        commonMessageId,
        commonRootMessageId: commonMessageId,
        commonReplyMessageId: undefined,
        content: [{ kind: 'text', text: 'hello' }],
        contentText: 'hello',
        scope: 'group',
    } as const;
}

describe('storeLarkInboundMessage', () => {
    beforeEach(() => {
        larkMessages.clear();
        commonMessages.clear();
        larkInsertCalls.length = 0;
        vectorizePublishes.length = 0;
        larkInsertRaceWinner = null;
    });

    it('writes the lark mapping once for a new om_id', async () => {
        await storeLarkInboundMessage(event('om_1') as any, projection('018f-common-1'), message);

        expect(larkMessages.get('om_1')?.common_message_id).toBe('018f-common-1');
        expect(larkInsertCalls.length).toBe(1);
        expect(vectorizePublishes.length).toBe(1);
    });

    it('skips lark_message insert when the om_id already maps to the same common id', async () => {
        commonMessages.add('018f-common-1');
        larkMessages.set('om_1', {
            om_id: 'om_1',
            common_message_id: '018f-common-1',
        });

        await storeLarkInboundMessage(event('om_1') as any, projection('018f-common-1'), message);

        expect(larkInsertCalls.length).toBe(0);
        expect(vectorizePublishes.length).toBe(0);
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
        expect(vectorizePublishes.length).toBe(0);
    });
});
