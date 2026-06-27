import { afterEach, beforeEach, describe, expect, it, mock } from 'bun:test';

// ---- in-memory stores backing the mocked ormconfig ----
const qqUsers = new Map<string, Record<string, unknown>>();
const commonUsers = new Map<string, Record<string, unknown>>();
const qqConvs = new Map<string, Record<string, unknown>>();
const commonConvs = new Map<string, Record<string, unknown>>();
const qqMessages = new Map<string, { qq_message_id: string; common_message_id: string }>();
const commonMessages = new Set<string>();
const commonMessageRows = new Map<string, Record<string, unknown>>();
const commonInsertCalls: Record<string, unknown>[] = [];

function userKey(row: Record<string, unknown>): string {
    return `${row.botName}|${row.scopeKey}|${row.openId}`;
}

// 模拟并发首投影里 ensureQqCommonUser 的「决策读」拿到陈旧空读（findOne 看不到
// 并发对手刚写入的行）。修好后的实现不再用 findOne 决策、改 insert-or-ignore +
// 读回 canonical，所以这个开关只影响旧（buggy）实现，用来复现并发覆盖+孤儿。
let staleQqUserFindOne = false;

// 同上，但针对 ensureQqCommonConversation 的「决策读」。复现并发首投影里第二个
// racer 拿到陈旧空读 → 重复造 common_conversation + 覆盖私表映射 + 孤儿会话。
// 修好后的实现改 insert-or-ignore + 读回 canonical，这个开关只影响旧 buggy 实现。
let staleQqConvFindOne = false;

function simpleRepo(map: Map<string, Record<string, unknown>>, pk: string) {
    return {
        findOne: mock(async ({ where }: { where: Record<string, unknown> }) => {
            for (const row of map.values()) {
                if (Object.entries(where).every(([k, v]) => row[k] === v)) return row;
            }
            return null;
        }),
        findOneOrFail: mock(async ({ where }: { where: Record<string, unknown> }) => {
            for (const row of map.values()) {
                if (Object.entries(where).every(([k, v]) => row[k] === v)) return row;
            }
            throw new Error('not found');
        }),
        upsert: mock(async (value: Record<string, unknown>) => {
            const key = String(value[pk]);
            map.set(key, { ...(map.get(key) ?? {}), ...value });
            return { identifiers: [] };
        }),
        update: mock(async (where: Record<string, unknown>, patch: Record<string, unknown>) => {
            for (const [k, row] of map) {
                if (Object.entries(where).every(([f, v]) => row[f] === v)) {
                    map.set(k, { ...row, ...patch });
                    return { affected: 1 };
                }
            }
            return { affected: 0 };
        }),
    };
}

// QqUserOpenId 的 insert ... ON CONFLICT DO NOTHING（orIgnore）模拟：键已存在不覆盖。
function qqUserInsertBuilder() {
    let payload: Record<string, unknown> = {};
    return {
        insert() {
            return this;
        },
        into() {
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
            const key = userKey(payload);
            if (qqUsers.has(key)) return { identifiers: [] }; // DO NOTHING
            qqUsers.set(key, { ...payload });
            return { identifiers: [{ open_id: payload.openId }] };
        },
    };
}

function userRepo() {
    return {
        findOne: mock(async ({ where }: { where: Record<string, unknown> }) => {
            if (staleQqUserFindOne) return null;
            for (const row of qqUsers.values()) {
                if (Object.entries(where).every(([k, v]) => row[k] === v)) return row;
            }
            return null;
        }),
        findOneOrFail: mock(async ({ where }: { where: Record<string, unknown> }) => {
            for (const row of qqUsers.values()) {
                if (Object.entries(where).every(([k, v]) => row[k] === v)) return row;
            }
            throw new Error('not found');
        }),
        upsert: mock(async (value: Record<string, unknown>) => {
            qqUsers.set(userKey(value), { ...(qqUsers.get(userKey(value)) ?? {}), ...value });
            return { identifiers: [] };
        }),
        createQueryBuilder: () => qqUserInsertBuilder(),
    };
}

// QqGroupChatInfo 的 insert ... ON CONFLICT DO NOTHING（orIgnore）模拟：键已存在不覆盖。
function qqConvInsertBuilder() {
    let payload: Record<string, unknown> = {};
    return {
        insert() {
            return this;
        },
        into() {
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
            const key = String(payload.conversation_id);
            if (qqConvs.has(key)) return { identifiers: [] }; // DO NOTHING
            qqConvs.set(key, { ...payload });
            return { identifiers: [{ conversation_id: key }] };
        },
    };
}

function qqConvRepo() {
    return {
        findOne: mock(async ({ where }: { where: Record<string, unknown> }) => {
            if (staleQqConvFindOne) return null;
            for (const row of qqConvs.values()) {
                if (Object.entries(where).every(([k, v]) => row[k] === v)) return row;
            }
            return null;
        }),
        findOneOrFail: mock(async ({ where }: { where: Record<string, unknown> }) => {
            for (const row of qqConvs.values()) {
                if (Object.entries(where).every(([k, v]) => row[k] === v)) return row;
            }
            throw new Error('not found');
        }),
        upsert: mock(async (value: Record<string, unknown>) => {
            const key = String(value.conversation_id);
            qqConvs.set(key, { ...(qqConvs.get(key) ?? {}), ...value });
            return { identifiers: [] };
        }),
        update: mock(async (where: Record<string, unknown>, patch: Record<string, unknown>) => {
            for (const [k, row] of qqConvs) {
                if (Object.entries(where).every(([f, v]) => row[f] === v)) {
                    qqConvs.set(k, { ...row, ...patch });
                    return { affected: 1 };
                }
            }
            return { affected: 0 };
        }),
        createQueryBuilder: () => qqConvInsertBuilder(),
    };
}

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
                if (commonMessages.has(id)) return { identifiers: [] };
                commonMessages.add(id);
                commonMessageRows.set(id, payload);
                return { identifiers: [{ common_message_id: id }] };
            }
            if (target?.name === 'QqMessage') {
                const qid = payload.qq_message_id as string;
                if (!qqMessages.has(qid)) {
                    qqMessages.set(qid, {
                        qq_message_id: qid,
                        common_message_id: payload.common_message_id as string,
                    });
                }
                return { identifiers: [{ qq_message_id: qid }] };
            }
            throw new Error(`unexpected insert target: ${target?.name}`);
        },
    };
}

mock.module('ormconfig', () => ({
    default: {
        getRepository: (entity: { name?: string }) => {
            if (entity.name === 'QqUserOpenId') return userRepo();
            if (entity.name === 'CommonUser') return simpleRepo(commonUsers, 'common_user_id');
            if (entity.name === 'QqGroupChatInfo') return qqConvRepo();
            if (entity.name === 'CommonConversation')
                return simpleRepo(commonConvs, 'common_conversation_id');
            if (entity.name === 'QqMessage') {
                return {
                    findOne: mock(async ({ where }: { where: { qq_message_id?: string; common_message_id?: string } }) => {
                        if (where.qq_message_id) return qqMessages.get(where.qq_message_id) ?? null;
                        if (where.common_message_id) {
                            for (const row of qqMessages.values())
                                if (row.common_message_id === where.common_message_id) return row;
                        }
                        return null;
                    }),
                };
            }
            if (entity.name === 'CommonMessage') {
                return {
                    update: mock(async (where: { common_message_id: string; role: string }, patch: Record<string, unknown>) => {
                        const row = commonMessageRows.get(where.common_message_id);
                        if (!row || row.role !== where.role) return { affected: 0 };
                        commonMessageRows.set(where.common_message_id, { ...row, ...patch });
                        return { affected: 1 };
                    }),
                };
            }
            throw new Error(`unexpected repo: ${entity.name}`);
        },
        transaction: async (task: (m: unknown) => Promise<void>) => {
            return task({
                getRepository: (entity: { name?: string }) => {
                    if (entity.name === 'QqMessage') {
                        return {
                            findOne: mock(async ({ where }: { where: { qq_message_id: string } }) =>
                                qqMessages.get(where.qq_message_id) ?? null,
                            ),
                        };
                    }
                    throw new Error(`unexpected tx repo: ${entity.name}`);
                },
                createQueryBuilder: () => insertBuilder(),
            });
        },
    },
}));

mock.module('@cache/redis-client', () => ({
    setNx: mock(async () => 'OK'),
    evalScript: mock(async () => 1),
}));
mock.module('@middleware/context', () => ({
    context: { getBotName: () => 'chiwei-qq', getLane: () => undefined },
}));
mock.module('@core/services/bot/multi-bot-manager', () => ({
    multiBotManager: { getBotCommonUserId: () => 'bot-common-user' },
}));

const {
    ensureQqCommonUser,
    ensureQqCommonConversation,
    storeQqInboundMessage,
    storeQqOutboundMessage,
} = await import('./common-projector');

function reset() {
    qqUsers.clear();
    commonUsers.clear();
    qqConvs.clear();
    commonConvs.clear();
    qqMessages.clear();
    commonMessages.clear();
    commonMessageRows.clear();
    commonInsertCalls.length = 0;
    staleQqUserFindOne = false;
    staleQqConvFindOne = false;
}

beforeEach(reset);
afterEach(reset);

describe('ensureQqCommonUser', () => {
    it('same (bot, direct, openId) is a stable common_user across calls', async () => {
        const a = await ensureQqCommonUser({
            botName: 'chiwei-qq',
            scope: 'direct',
            conversationId: 'c2c_1',
            openId: 'u_1',
            displayName: '主人',
        });
        const b = await ensureQqCommonUser({
            botName: 'chiwei-qq',
            scope: 'direct',
            conversationId: 'c2c_1',
            openId: 'u_1',
            displayName: '主人',
        });
        expect(a).toBe(b);
    });

    it('direct user_openid and a group member_openid with the same string never mix', async () => {
        const direct = await ensureQqCommonUser({
            botName: 'chiwei-qq',
            scope: 'direct',
            conversationId: 'c2c_1',
            openId: 'same_string',
            displayName: 'X',
        });
        const group = await ensureQqCommonUser({
            botName: 'chiwei-qq',
            scope: 'group',
            conversationId: 'group_1',
            openId: 'same_string',
            displayName: 'X',
        });
        expect(direct).not.toBe(group);
    });

    it('same member in two different groups maps to two different common_users', async () => {
        const g1 = await ensureQqCommonUser({
            botName: 'chiwei-qq',
            scope: 'group',
            conversationId: 'group_1',
            openId: 'm_1',
            displayName: 'M',
        });
        const g2 = await ensureQqCommonUser({
            botName: 'chiwei-qq',
            scope: 'group',
            conversationId: 'group_2',
            openId: 'm_1',
            displayName: 'M',
        });
        expect(g1).not.toBe(g2);
    });

    it('concurrent first projection converges to the first writer (no overwrite, no orphan)', async () => {
        // racer 1 已提交：qq_user_open_id 行 + 它的 common_user。
        const scopeKey = 'group:group_x';
        qqUsers.set('chiwei-qq|group:group_x|m_race', {
            botName: 'chiwei-qq',
            scopeKey,
            openId: 'm_race',
            commonUserId: 'common-A',
        });
        commonUsers.set('common-A', { common_user_id: 'common-A', channel: 'qq' });

        // racer 2 首投影：决策读拿到陈旧空读（并发窗口）。修好后应收敛到 common-A、
        // 不覆盖、不产生第二个 CommonUser。
        staleQqUserFindOne = true;
        const got = await ensureQqCommonUser({
            botName: 'chiwei-qq',
            scope: 'group',
            conversationId: 'group_x',
            openId: 'm_race',
            displayName: undefined,
        });

        expect(got).toBe('common-A');
        expect(commonUsers.size).toBe(1);
        expect(qqUsers.get('chiwei-qq|group:group_x|m_race')!.commonUserId).toBe('common-A');
    });
});

describe('ensureQqCommonConversation', () => {
    it('same conversationId yields a stable common_conversation across calls', async () => {
        const a = await ensureQqCommonConversation({
            conversationId: 'group_1',
            scope: 'group',
            botName: 'chiwei-qq',
            displayName: '群',
        });
        const b = await ensureQqCommonConversation({
            conversationId: 'group_1',
            scope: 'group',
            botName: 'chiwei-qq',
            displayName: '群',
        });
        expect(a).toBe(b);
    });

    it('different conversations get different common_conversations', async () => {
        const a = await ensureQqCommonConversation({
            conversationId: 'group_1',
            scope: 'group',
            botName: 'chiwei-qq',
        });
        const b = await ensureQqCommonConversation({
            conversationId: 'c2c_1',
            scope: 'direct',
            botName: 'chiwei-qq',
        });
        expect(a).not.toBe(b);
    });

    it('concurrent first projection converges to the first writer (no second common_conversation, no overwrite)', async () => {
        // racer 1 已提交：qq_group_chat_info 行 + 它的 common_conversation。
        qqConvs.set('group_race', {
            conversation_id: 'group_race',
            scope: 'group',
            bot_name: 'chiwei-qq',
            common_conversation_id: 'conv-A',
        });
        commonConvs.set('conv-A', { common_conversation_id: 'conv-A', channel: 'qq' });

        // racer 2 首投影：决策读拿到陈旧空读（并发窗口）。修好后应收敛到 conv-A、
        // 不覆盖私表映射、不产生第二个 CommonConversation。
        staleQqConvFindOne = true;
        const got = await ensureQqCommonConversation({
            conversationId: 'group_race',
            scope: 'group',
            botName: 'chiwei-qq',
            displayName: undefined,
        });

        expect(got).toBe('conv-A');
        expect(commonConvs.size).toBe(1);
        expect(qqConvs.get('group_race')!.common_conversation_id).toBe('conv-A');
    });

    it('refreshes metadata on an existing conversation', async () => {
        const id = await ensureQqCommonConversation({
            conversationId: 'group_meta',
            scope: 'group',
            botName: 'chiwei-qq',
            displayName: '旧名',
            memberCount: 3,
        });
        await ensureQqCommonConversation({
            conversationId: 'group_meta',
            scope: 'group',
            botName: 'chiwei-qq',
            displayName: '新名',
            memberCount: 5,
        });
        expect(commonConvs.get(id)!.display_name).toBe('新名');
        expect(commonConvs.get(id)!.member_count).toBe(5);
    });
});

describe('storeQqInboundMessage', () => {
    const projection = {
        commonUserId: 'cu-1',
        commonConversationId: 'cc-1',
        commonMessageId: 'cm-1',
        commonRootMessageId: 'cm-1',
        commonReplyMessageId: undefined,
        mentionedUserIds: [],
        content: [{ kind: 'text' as const, text: 'hi' }],
        contentText: 'hi',
        scope: 'group',
    };

    function inbound() {
        return {
            channel: 'qq',
            bot_name: 'chiwei-qq',
            channel_message_id: 'qq_1',
            channel_chat_id: 'group_1',
            channel_user_id: 'm_1',
            conversation_scope: 'group',
            thread_ref: null,
            addressing_hints: [],
            content: projection.content,
            received_at: 1780000000000,
        } as any;
    }

    it('writes the qq mapping once for a new qq message id', async () => {
        await storeQqInboundMessage(inbound(), projection);
        expect(qqMessages.get('qq_1')?.common_message_id).toBe('cm-1');
        expect(commonInsertCalls.length).toBe(1);
    });

    it('fails loud when a qq message id already maps to a different common id', async () => {
        qqMessages.set('qq_1', { qq_message_id: 'qq_1', common_message_id: 'cm-other' });
        await expect(storeQqInboundMessage(inbound(), projection)).rejects.toThrow(
            /already maps to cm-other/,
        );
    });
});

describe('storeQqOutboundMessage', () => {
    it('writes an assistant common_message with the bot common user id', async () => {
        await storeQqOutboundMessage({
            qqMessageId: 'qq_out_1',
            conversationId: 'group_1',
            commonConversationId: 'cc-1',
            commonRootMessageId: undefined,
            commonReplyMessageId: undefined,
            contentText: '回复',
            botName: 'chiwei-qq',
            scope: 'group',
            eventTime: 1780000000000,
            messageType: 'text',
            responseId: 'resp-1',
        });
        expect(commonInsertCalls[0].role).toBe('assistant');
        expect(commonInsertCalls[0].common_user_id).toBe('bot-common-user');
    });
});
