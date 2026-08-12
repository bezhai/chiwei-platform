// 九条斜杠子指令的本体。
//
// 它们几乎全是"读一下、回一句话"，所以能钉的就是**那句话本身**和**读的是哪一行**。两者
// 错了都不报错：文案错了只是措辞变了（但那些字是线上历史），读错行会让管理员按一个假的
// 答案去做决定（比如以为某人已经被拉黑）。
//
// 三处准入口径必须分开看，也分开钉：
//
//   `/block` `/unblock` `/blocklist`   handler 里各判一次管理员，非管理员收到一句拒绝
//   其余六条                            **不判**
//   （对比「余额」那条：IsAdmin 在 rules 里，非管理员是不命中）

import { describe, expect, it } from 'bun:test';

import type { LarkContentPart } from '../message/lark-content';
import { resolveLarkMentions, type LarkBotLookup } from '../message/mentions';
import type { LarkInboundMessage } from '../message/parse-message';
import type { LarkMention } from '../message/wire';
import type { LarkGroupBinding, LarkGroupMemberRow, LarkMessageRow } from '../projection/tables';
import type { LarkCommandContext } from '../rules/command-context';
import type { LarkCommandDeps, LarkSlashCommand } from '../rules/commands';
import { bindCommand, unbindCommand } from './slash-bind';
import { blockCommand, blocklistCommand, unblockCommand } from './slash-block';
import { chatIdCommand, messageIdCommand, unionIdCommand } from './slash-ids';
import { sessionCommand } from './slash-session';
import type { LarkMessageSession } from './slash-tables';

const BOT_APP_ID = 'cli_tool';

const bots: LarkBotLookup = {
    byAppId: (appId) =>
        appId === BOT_APP_ID
            ? { botName: 'tool', displayName: null, commonUserId: 'cu_bot_tool' }
            : null,
    byUnionId: () => null,
};

interface World {
    /** 库里现有的东西。 */
    member?: LarkGroupMemberRow | null;
    binding?: LarkGroupBinding | null;
    larkMessage?: LarkMessageRow | null;
    session?: LarkMessageSession | null;
    blocked?: string[];
    /** 飞书查人抛错（多半是应用没通讯录权限）。 */
    userLookupFails?: string;
    /** 这条消息的事实。 */
    parentId?: string;
    isAdmin?: boolean;
    mentions?: LarkMention[];
}

interface Did {
    said: string[];
    inThread: boolean[];
    bindings: string[];
    blocklist: string[];
    lookedUp: string[];
}

function rig(world: World = {}) {
    const did: Did = { said: [], inThread: [], bindings: [], blocklist: [], lookedUp: [] };
    const blocked = new Set(world.blocked ?? []);

    // `deps.database` 的替身：三条黑名单指令在装配期从它建仓储。TypeORM 的仓储只被用到
    // 四个方法，其余留空 —— 用到了就会当场炸，比悄悄返回 undefined 诚实。
    const database = {
        getRepository: () => ({
            findOne: async ({ where }: { where: { union_id?: string; common_message_id?: string } }) => {
                if (where.common_message_id !== undefined) {
                    // 这个替身说的是**实体的列名**，不是端口的字段名 —— 它站在
                    // postgresAgentSessions 底下，翻译那一步正是被测的东西。
                    return world.session
                        ? { role: world.session.role, response_id: world.session.responseId }
                        : null;
                }
                return blocked.has(where.union_id!) ? { union_id: where.union_id } : null;
            },
            save: async (row: { union_id: string; blocked_by: string }) => {
                did.blocklist.push(`block ${row.union_id} by ${row.blocked_by}`);
                blocked.add(row.union_id);
            },
            delete: async (where: { union_id: string }) => {
                did.blocklist.push(`unblock ${where.union_id}`);
                blocked.delete(where.union_id);
            },
            find: async () => [...blocked].map((union_id) => ({ union_id })),
        }),
    };

    const deps = {
        api: {
            replyText: async (_messageId: string, text: string, inThread: boolean) => {
                did.said.push(text);
                did.inThread.push(inThread);
                return {};
            },
            getUser: async (unionId: string) => {
                did.lookedUp.push(unionId);
                if (world.userLookupFails) throw new Error(world.userLookupFails);
                return { unionId };
            },
        },
        store: {
            larkGroupMember: async () => world.member ?? null,
            larkGroupBinding: async () => world.binding ?? null,
            larkMessage: async () => world.larkMessage ?? null,
            insertLarkGroupBinding: async (chatId: string, unionId: string) => {
                did.bindings.push(`insert ${unionId}@${chatId}`);
            },
            setLarkGroupBindingActive: async (
                chatId: string,
                unionId: string,
                isActive: boolean,
            ) => {
                did.bindings.push(`${isActive ? 'activate' : 'deactivate'} ${unionId}@${chatId}`);
            },
        },
        database,
    } as unknown as LarkCommandDeps;

    const message: LarkInboundMessage = {
        messageId: 'om_trigger',
        chatId: 'oc_1',
        chatType: 'group',
        messageType: 'text',
        createTime: '1700000000000',
        parentId: world.parentId,
        appId: BOT_APP_ID,
        sender: { unionId: 'on_admin', openId: 'ou_admin' },
        mentions: world.mentions ?? [],
        segments: [],
    };

    const context: LarkCommandContext = {
        message,
        mentions: resolveLarkMentions(world.mentions ?? [], bots),
        content: [] as LarkContentPart[],
        projection: {
            commonUserId: 'cu_sender',
            commonConversationId: 'cc_1',
            commonMessageId: 'cm_1',
            commonRootMessageId: 'cm_root',
            commonReplyMessageId: undefined,
            mentionedCommonUserIds: [],
        },
        botName: 'tool',
        appId: BOT_APP_ID,
        isAdmin: world.isAdmin ?? true,
        permission: {},
        groupChat: null,
    };

    const run = async (build: (deps: LarkCommandDeps) => LarkSlashCommand) => {
        await build(deps)(null as never, context);
        return did;
    };

    return { did, run };
}

/** @ 了一个真人。 */
function human(unionId = 'on_zhangsan'): LarkMention[] {
    return [{ key: '@_user_1', id: { union_id: unionId }, name: '张三' }];
}

/** 只 @ 了我们自己的 bot —— 对这几条指令来说等于"没 @ 到人"。 */
function onlyTheBot(): LarkMention[] {
    return [
        {
            key: '@_user_1',
            id: { union_id: 'on_bot' },
            name: 'tool',
            mentioned_type: 'bot',
            bot_info: { app_id: BOT_APP_ID },
        },
    ];
}

// ---------------------------------------------------------------------------

describe('/chat_id、/message_id、/union_id', () => {
    it('/chat_id 念这个会话的 chat_id', async () => {
        const { did, run } = rig();
        await run(chatIdCommand);
        expect(did.said).toEqual(['oc_1']);
        // 九条回复全部进话题（上游 replyMessage 的第三个参数是 true）。
        expect(did.inThread).toEqual([true]);
    });

    it('/message_id 念被回复那条的 id，不是自己的', async () => {
        const { did, run } = rig({ parentId: 'om_parent' });
        await run(messageIdCommand);
        expect(did.said).toEqual(['om_parent']);
    });

    it('/message_id 没回复任何消息时说消息不存在', async () => {
        const { did, run } = rig();
        await run(messageIdCommand);
        expect(did.said).toEqual(['消息不存在']);
    });

    it('/union_id 念第一个被 @ 的真人', async () => {
        const { did, run } = rig({ mentions: human() });
        await run(unionIdCommand);
        expect(did.said).toEqual(['union_id: on_zhangsan']);
    });

    it('/union_id 只 @ 了 bot 时当作没 @ 人', async () => {
        const { did, run } = rig({ mentions: onlyTheBot() });
        await run(unionIdCommand);
        expect(did.said).toEqual(['请@具体用户进行获取union_id']);
    });

    // 这三条不判管理员，与拆分前一致 —— 会话 id 在群里本来就人人可见。
    it('这三条不判管理员', async () => {
        const { did, run } = rig({ isAdmin: false, mentions: human() });
        await run(chatIdCommand);
        await run(unionIdCommand);
        expect(did.said).toEqual(['oc_1', 'union_id: on_zhangsan']);
    });
});

describe('/bind', () => {
    const inGroup: LarkGroupMemberRow = {
        chat_id: 'oc_1',
        union_id: 'on_zhangsan',
        is_leave: false,
    };

    it('第一次绑：查飞书、查群成员、插一行', async () => {
        const { did, run } = rig({ mentions: human(), member: inGroup, binding: null });

        await run(bindCommand);

        expect(did.lookedUp).toEqual(['on_zhangsan']);
        expect(did.bindings).toEqual(['insert on_zhangsan@oc_1']);
        expect(did.said).toEqual(['绑定成功，该用户退群后将被自动重新拉回群聊']);
    });

    // 解绑过的行复用而不是再插一条：那张表上没有唯一约束，插第二条就多一行。
    it('绑过又解绑过的：把那一行打开，不插新的', async () => {
        const { did, run } = rig({
            mentions: human(),
            member: inGroup,
            binding: { user_union_id: 'on_zhangsan', chat_id: 'oc_1', is_active: false },
        });

        await run(bindCommand);

        expect(did.bindings).toEqual(['activate on_zhangsan@oc_1']);
        expect(did.said).toEqual(['绑定成功，该用户退群后将被自动重新拉回群聊']);
    });

    it('已经绑着的：什么也不写', async () => {
        const { did, run } = rig({
            mentions: human(),
            member: inGroup,
            binding: { user_union_id: 'on_zhangsan', chat_id: 'oc_1', is_active: true },
        });

        await run(bindCommand);

        expect(did.bindings).toEqual([]);
        expect(did.said).toEqual(['该用户已绑定，无需重复绑定']);
    });

    it('没 @ 人：连飞书都不查', async () => {
        const { did, run } = rig();
        await run(bindCommand);
        expect(did.lookedUp).toEqual([]);
        expect(did.said).toEqual(['请@具体用户进行绑定']);
    });

    // 上游那次 findOne 带着 `is_leave: false`，所以退了群的人在这里就是"不在群中"。
    it('退过群的人算不在群里', async () => {
        const { did, run } = rig({
            mentions: human(),
            member: { ...inGroup, is_leave: true },
        });

        await run(bindCommand);

        expect(did.bindings).toEqual([]);
        expect(did.said).toEqual(['该用户不在群中，无法绑定']);
    });

    it('从来没进过这个群的人也一样', async () => {
        const { did, run } = rig({ mentions: human(), member: null });
        await run(bindCommand);
        expect(did.said).toEqual(['该用户不在群中，无法绑定']);
    });

    // 飞书那次查人只看它抛不抛：抛了把原话转达（多半是应用没通讯录权限）。
    it('飞书查人抛错时把原话转达给管理员', async () => {
        const { did, run } = rig({
            mentions: human(),
            member: inGroup,
            userLookupFails: '应用未开通通讯录权限',
        });

        await run(bindCommand);

        expect(did.bindings).toEqual([]);
        expect(did.said).toEqual(['应用未开通通讯录权限']);
    });

    it('不判管理员', async () => {
        const { did, run } = rig({ isAdmin: false, mentions: human(), member: inGroup });
        await run(bindCommand);
        expect(did.bindings).toEqual(['insert on_zhangsan@oc_1']);
    });
});

describe('/unbind', () => {
    it('绑着的：软删（只把 is_active 关掉）', async () => {
        const { did, run } = rig({
            mentions: human(),
            binding: { user_union_id: 'on_zhangsan', chat_id: 'oc_1', is_active: true },
        });

        await run(unbindCommand);

        expect(did.bindings).toEqual(['deactivate on_zhangsan@oc_1']);
        expect(did.said).toEqual(['解绑成功，该用户退群后将不会被自动拉回群聊']);
    });

    it('从来没绑过：什么也不写', async () => {
        const { did, run } = rig({ mentions: human(), binding: null });
        await run(unbindCommand);
        expect(did.bindings).toEqual([]);
        expect(did.said).toEqual(['该用户未绑定，无需解绑']);
    });

    it('已经解绑过的也一样', async () => {
        const { did, run } = rig({
            mentions: human(),
            binding: { user_union_id: 'on_zhangsan', chat_id: 'oc_1', is_active: false },
        });

        await run(unbindCommand);

        expect(did.bindings).toEqual([]);
        expect(did.said).toEqual(['该用户未绑定，无需解绑']);
    });

    it('没 @ 人', async () => {
        const { did, run } = rig();
        await run(unbindCommand);
        expect(did.said).toEqual(['请@具体用户进行解绑']);
    });

    // /unbind 不查群成员：人已经退群了才更需要解绑。
    it('不查这个人还在不在群里', async () => {
        const { did, run } = rig({
            mentions: human(),
            member: null,
            binding: { user_union_id: 'on_zhangsan', chat_id: 'oc_1', is_active: true },
        });

        await run(unbindCommand);

        expect(did.bindings).toEqual(['deactivate on_zhangsan@oc_1']);
    });
});

describe('/block、/unblock、/blocklist', () => {
    it('拉黑：写 union_id 和操作人', async () => {
        const { did, run } = rig({ mentions: human() });

        await run(blockCommand);

        expect(did.blocklist).toEqual(['block on_zhangsan by on_admin']);
        expect(did.said).toEqual(['拉黑成功']);
    });

    it('已经在名单里就不重复写', async () => {
        const { did, run } = rig({ mentions: human(), blocked: ['on_zhangsan'] });

        await run(blockCommand);

        expect(did.blocklist).toEqual([]);
        expect(did.said).toEqual(['该用户已在黑名单中']);
    });

    it('解除拉黑：删那一行', async () => {
        const { did, run } = rig({ mentions: human(), blocked: ['on_zhangsan'] });

        await run(unblockCommand);

        expect(did.blocklist).toEqual(['unblock on_zhangsan']);
        expect(did.said).toEqual(['解除拉黑成功']);
    });

    it('不在名单里就不删', async () => {
        const { did, run } = rig({ mentions: human() });

        await run(unblockCommand);

        expect(did.blocklist).toEqual([]);
        expect(did.said).toEqual(['该用户不在黑名单中']);
    });

    it('列名单：从 1 开始编号', async () => {
        const { did, run } = rig({ blocked: ['on_a', 'on_b'] });

        await run(blocklistCommand);

        expect(did.said).toEqual(['黑名单列表:\n1. on_a\n2. on_b']);
    });

    it('名单为空', async () => {
        const { did, run } = rig();
        await run(blocklistCommand);
        expect(did.said).toEqual(['黑名单为空']);
    });

    // 三条各自的拒绝话术不一样，所以逐条钉 —— 抽一个公共 gate 就得统一措辞。
    it('非管理员：三句各不相同的拒绝，而且什么都不做', async () => {
        const { did, run } = rig({ isAdmin: false, mentions: human(), blocked: ['on_zhangsan'] });

        await run(blockCommand);
        await run(unblockCommand);
        await run(blocklistCommand);

        expect(did.said).toEqual([
            '只有管理员可以拉黑用户',
            '只有管理员可以解除拉黑',
            '只有管理员可以查看黑名单',
        ]);
        expect(did.blocklist).toEqual([]);
    });

    it('管理员但没 @ 人', async () => {
        const { did, run } = rig();

        await run(blockCommand);
        await run(unblockCommand);

        expect(did.said).toEqual(['请@具体用户进行拉黑', '请@具体用户进行解除拉黑']);
    });
});

describe('/session', () => {
    const mapping: LarkMessageRow = {
        om_id: 'om_parent',
        common_message_id: 'cm_parent',
        chat_id: 'oc_1',
        message_type: 'text',
    };

    it('回复赤尾说的一句：念出那次对话的 session_id', async () => {
        const { did, run } = rig({
            parentId: 'om_parent',
            larkMessage: mapping,
            session: { role: 'assistant', responseId: 'sess-1' },
        });

        await run(sessionCommand);

        expect(did.said).toEqual(['找到啦！session_id 是：\nsess-1']);
    });

    it('没回复任何消息', async () => {
        const { did, run } = rig();
        await run(sessionCommand);
        expect(did.said).toEqual([
            '人家找不到要查询的消息啦，请回复人家说的某条消息再试试～',
        ]);
    });

    // 两段查不到说的是同一句话，与拆分前一致。
    it('lark_message 与 common_message 任一段查不到都是同一句话', async () => {
        const missingMapping = rig({ parentId: 'om_parent', larkMessage: null });
        await missingMapping.run(sessionCommand);

        const missingRow = rig({ parentId: 'om_parent', larkMessage: mapping, session: null });
        await missingRow.run(sessionCommand);

        expect(missingMapping.did.said).toEqual(['唔...这条消息人家不认识，找不到记录呢 (´•ω•`)']);
        expect(missingRow.did.said).toEqual(missingMapping.did.said);
    });

    it('回复的是真人说的话', async () => {
        const { did, run } = rig({
            parentId: 'om_parent',
            larkMessage: mapping,
            session: { role: 'user', responseId: 'sess-1' },
        });

        await run(sessionCommand);

        expect(did.said).toEqual(['这条消息不是人家发的哦，要回复人家的消息才能查 session 呀～']);
    });

    // 主动发的消息（睡前那种）不挂在任何一次台账上。
    it('赤尾说的、但没有 response_id', async () => {
        const { did, run } = rig({
            parentId: 'om_parent',
            larkMessage: mapping,
            session: { role: 'assistant', responseId: undefined },
        });

        await run(sessionCommand);

        expect(did.said).toEqual(['找不到对应的触发消息，session 不见了呢 (；´д｀)']);
    });

    it('不判管理员', async () => {
        const { did, run } = rig({
            isAdmin: false,
            parentId: 'om_parent',
            larkMessage: mapping,
            session: { role: 'assistant', responseId: 'sess-1' },
        });

        await run(sessionCommand);

        expect(did.said).toEqual(['找到啦！session_id 是：\nsess-1']);
    });
});
