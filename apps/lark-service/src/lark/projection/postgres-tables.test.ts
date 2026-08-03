// 端口的真身发出去的到底是什么 SQL。
//
// 投影的**逻辑**由 inbound-projection.test.ts 用内存实现验；那份实现再怎么正确，
// 也说明不了真身有没有写错表、漏掉 ON CONFLICT、或者忘了 ORDER BY。开发机连不到
// 数据库，所以这里换个办法：用真的 TypeORM + 真的实体元数据建一个**不连库**的
// DataSource，把 query runner 换成录音机，跑一遍就能拿到真实的 SQL 与参数。
//
// 这样拿到的不是"我以为它会生成什么"，是 TypeORM 真的生成了什么。

import { beforeEach, describe, expect, it } from 'bun:test';
import { DataSource } from 'typeorm';
import { Broadcaster } from 'typeorm/subscriber/Broadcaster.js';

import { LARK_SERVICE_ENTITIES } from '../../ormconfig';
import { postgresLarkTables } from './postgres-tables';
import type { LarkStore } from './tables';

interface Recorded {
    sql?: string;
    params?: unknown[];
    tx?: 'begin' | 'commit' | 'rollback';
}

interface Harness {
    store: LarkStore;
    recorded: Recorded[];
    /** 下一条 SELECT 返回什么（原始列名，形如 `Alias_column`）。 */
    reply(rows: Array<Record<string, unknown>>): void;
    sqlOf(fragment: string): Recorded;
    statements(): string[];
}

function harness(): Harness {
    const dataSource = new DataSource({
        type: 'postgres',
        host: 'unused',
        port: 5432,
        username: 'unused',
        password: 'unused',
        database: 'unused',
        synchronize: false,
        entities: LARK_SERVICE_ENTITIES,
    });
    // 只解析元数据，不连库 —— initialize() 在建连接之前做的正是这一步。
    (dataSource as unknown as { buildMetadatas(): void }).buildMetadatas();
    (dataSource as unknown as { isInitialized: boolean }).isInitialized = true;

    const recorded: Recorded[] = [];
    let pending: Array<Record<string, unknown>> = [];
    const runner: Record<string, unknown> = {
        connection: dataSource,
        isReleased: false,
        isTransactionActive: false,
        data: {},
        connect: async () => {},
        release: async () => {},
        startTransaction: async () => {
            recorded.push({ tx: 'begin' });
            runner.isTransactionActive = true;
        },
        commitTransaction: async () => {
            recorded.push({ tx: 'commit' });
            runner.isTransactionActive = false;
        },
        rollbackTransaction: async () => {
            recorded.push({ tx: 'rollback' });
            runner.isTransactionActive = false;
        },
        query: async (sql: string, params: unknown[], structured?: boolean) => {
            recorded.push({ sql, params });
            const rows = pending;
            pending = [];
            return structured ? { records: rows, affected: rows.length, raw: rows } : rows;
        },
    };
    runner.broadcaster = new Broadcaster(runner as never);
    runner.manager = dataSource.createEntityManager(runner as never);
    dataSource.createQueryRunner = () => runner as never;

    return {
        store: postgresLarkTables(dataSource),
        recorded,
        reply: (rows) => {
            pending = rows;
        },
        sqlOf: (fragment) => {
            const hit = recorded.find((r) => r.sql?.includes(fragment));
            if (!hit) {
                throw new Error(
                    `no statement contains "${fragment}"; saw:\n` +
                        recorded.map((r) => r.tx ?? r.sql).join('\n'),
                );
            }
            return hit;
        },
        statements: () => recorded.map((r) => r.tx ?? r.sql!.split(' ').slice(0, 3).join(' ')),
    };
}

let h: Harness;
beforeEach(() => {
    h = harness();
});

describe('读：找已有的对应关系', () => {
    it('按 (app_id, open_id) 查 lark_user_open_id 并映射成列名口径的行', async () => {
        h.reply([
            {
                LarkUserOpenId_app_id: 'cli_a',
                LarkUserOpenId_open_id: 'ou_1',
                LarkUserOpenId_union_id: 'on_1',
                LarkUserOpenId_name: '张三',
                LarkUserOpenId_common_user_id: 'cu_1',
            },
        ]);

        const row = await h.store.larkUserByOpenId('cli_a', 'ou_1');

        expect(row).toEqual({
            app_id: 'cli_a',
            open_id: 'ou_1',
            union_id: 'on_1',
            name: '张三',
            common_user_id: 'cu_1',
        });
        const select = h.sqlOf('FROM "lark_user_open_id"');
        expect(select.sql).toContain('"app_id" = $1');
        expect(select.sql).toContain('"open_id" = $2');
        expect(select.params).toEqual(['cli_a', 'ou_1']);
    });

    it('查不到就是 null，不编一行出来', async () => {
        h.reply([]);
        expect(await h.store.larkUserByOpenId('cli_a', 'ou_1')).toBeNull();
    });

    // 一个 union_id 下挂着多条 open_id 行时，取哪一条必须是确定的 —— 不排序的话
    // 两个进程可能各取一条，把同一个人分成两半。
    it('按 union_id 查时按 common_user_id 升序取第一条', async () => {
        h.reply([]);
        await h.store.larkUserByUnionId('on_1');

        const select = h.sqlOf('FROM "lark_user_open_id"');
        expect(select.sql).toContain('"union_id" = $1');
        expect(select.sql).toContain('ORDER BY');
        expect(select.sql).toContain('"common_user_id" ASC');
        expect(select.sql).toContain('LIMIT 1');
    });

    it('发送者档案查 lark_user（消息事件里没有名字）', async () => {
        h.reply([{ LarkUser_union_id: 'on_1', LarkUser_name: '张三', LarkUser_avatar_origin: 'a' }]);

        expect(await h.store.larkUserProfile('on_1')).toEqual({
            name: '张三',
            avatar_origin: 'a',
        });
        expect(h.sqlOf('FROM "lark_user"').params).toEqual(['on_1']);
    });

    it('会话对应查 lark_base_chat_info', async () => {
        h.reply([
            {
                LarkBaseChatInfo_chat_id: 'oc_1',
                LarkBaseChatInfo_chat_mode: 'group',
                LarkBaseChatInfo_common_conversation_id: 'cc_1',
            },
        ]);

        expect(await h.store.larkChat('oc_1')).toEqual({
            chat_id: 'oc_1',
            chat_mode: 'group',
            common_conversation_id: 'cc_1',
        });
        expect(h.sqlOf('FROM "lark_base_chat_info"').params).toEqual(['oc_1']);
    });

    it('群资料查 lark_group_chat_info', async () => {
        h.reply([
            {
                LarkGroupChatInfo_chat_id: 'oc_1',
                LarkGroupChatInfo_name: '群',
                LarkGroupChatInfo_user_count: 3,
                LarkGroupChatInfo_is_leave: false,
                LarkGroupChatInfo_download_has_permission_setting: 'all_members',
            },
        ]);

        expect(await h.store.larkGroupChat('oc_1')).toEqual({
            name: '群',
            avatar: undefined,
            user_count: 3,
            is_leave: false,
            download_has_permission_setting: 'all_members',
        });
        expect(h.sqlOf('FROM "lark_group_chat_info"').params).toEqual(['oc_1']);
    });

    it('消息对应按 om_id 查 lark_message', async () => {
        h.reply([
            {
                LarkMessage_om_id: 'om_1',
                LarkMessage_common_message_id: 'cm_1',
                LarkMessage_chat_id: 'oc_1',
                LarkMessage_message_type: 'text',
            },
        ]);

        expect(await h.store.larkMessage('om_1')).toMatchObject({
            om_id: 'om_1',
            common_message_id: 'cm_1',
        });
        expect(h.sqlOf('FROM "lark_message"').params).toEqual(['om_1']);
    });
});

describe('写：每条语句的冲突语义', () => {
    it('common_user 按主键 upsert', async () => {
        await h.store.saveCommonUser({
            common_user_id: 'cu_1',
            channel: 'lark',
            display_name: '张三',
        });

        const insert = h.sqlOf('INSERT INTO "common_user"');
        expect(insert.sql).toContain('ON CONFLICT ( "common_user_id" ) DO UPDATE');
        expect(insert.params).toEqual(['cu_1', 'lark', '张三']);
    });

    // display_name 没查到的时候不该把库里已有的名字抹成空。TypeORM 的 upsert 只覆盖
    // "值不是 undefined"的列，这条把那个语义钉死。
    it('common_user 的 upsert 不会用空值覆盖已有的展示名', async () => {
        await h.store.saveCommonUser({
            common_user_id: 'cu_1',
            channel: 'lark',
            display_name: undefined,
        });

        const insert = h.sqlOf('INSERT INTO "common_user"');
        expect(insert.sql).not.toContain('"display_name" = EXCLUDED."display_name"');
        expect(insert.params).toEqual(['cu_1', 'lark']);
    });

    it('common_conversation 建行时按主键 upsert，字段齐全', async () => {
        await h.store.saveCommonConversation({
            common_conversation_id: 'cc_1',
            channel: 'lark',
            scope: 'group',
            display_name: '群',
            avatar_url: 'a',
            member_count: 3,
            is_active: true,
            attachment_policy: { download_allowed: true, source: 'lark' },
        });

        const insert = h.sqlOf('INSERT INTO "common_conversation"');
        expect(insert.sql).toContain('ON CONFLICT ( "common_conversation_id" ) DO UPDATE');
        expect(insert.params).toEqual([
            'cc_1',
            'lark',
            'group',
            '群',
            'a',
            3,
            true,
            '{"download_allowed":true,"source":"lark"}',
        ]);
    });

    it('common_conversation 每条消息幂等重写，查不到的那几项不进 SET', async () => {
        await h.store.saveCommonConversation({
            common_conversation_id: 'cc_1',
            channel: 'lark',
            scope: 'group',
            display_name: '新名字',
            avatar_url: undefined,
            member_count: 9,
            is_active: true,
            attachment_policy: { download_allowed: false, source: 'lark' },
        });

        const insert = h.sqlOf('INSERT INTO "common_conversation"');
        expect(insert.sql).toContain('ON CONFLICT ( "common_conversation_id" ) DO UPDATE');
        expect(insert.sql).toContain('"display_name" = EXCLUDED."display_name"');
        // 群资料表暂时没有这一行时，不该把公共层已有的头像抹成空
        expect(insert.sql).not.toContain('"avatar_url" = EXCLUDED."avatar_url"');
    });
});

// codex 指出的竞态：同一个人 / 同一个会话可以在**不同消息**里被并发地第一次创建，
// 按 om_id 取的锁保护不到。修法是让自然键自己回答"谁是 canonical"，而 COALESCE 是
// 这句话在 SQL 里的样子。
describe('认领：自然键首写者成为 canonical', () => {
    it('lark_user_open_id 的冲突子句保留已有的 common_user_id，只让候选值填空', async () => {
        h.reply([{ common_user_id: 'winner' }]);

        const canonical = await h.store.claimCommonUserId(
            { app_id: 'cli_a', open_id: 'ou_1' },
            { union_id: 'on_1', name: '张三' },
            'my_candidate',
        );

        // 返回的是库里最终生效的那一个，不是我们给的候选值
        expect(canonical).toBe('winner');

        const insert = h.sqlOf('INSERT INTO "lark_user_open_id"');
        expect(insert.sql).toContain('ON CONFLICT ("app_id", "open_id") DO UPDATE');
        expect(insert.sql).toContain(
            '"common_user_id" = COALESCE("lark_user_open_id"."common_user_id", ' +
                'EXCLUDED."common_user_id")',
        );
        // 可变列照常刷新（怎么刷新见下一条）
        expect(insert.sql).toContain('"union_id" = COALESCE(');
        expect(insert.sql).toContain('"name" = COALESCE(');
        // 拿不到赢家就没法继续，所以必须 RETURNING
        expect(insert.sql).toContain('RETURNING common_user_id');
        expect(insert.params).toEqual(['cli_a', 'ou_1', 'on_1', '张三', 'my_candidate']);
        // 上面那句 COALESCE 用表名限定引用目标行，这只在 INSERT 不带 alias 时合法。
        // TypeORM 在 `alias !== 表名` 时会补一个 `AS "..."`（InsertQueryBuilder），
        // 真出现的话 PG 会拒绝整条语句（invalid reference to FROM-clause entry）——
        // 而字符串断言照样通过。这条锁的就是那个前提。
        expect(insert.sql).not.toContain(' AS "');
    });

    // 同一件事的另一半：认领是"读在前、写在后"—— 调用方先读这一行、读不到就把空值
    // 传进来。并发的另一条流刚把有效的 union_id 写进去时，本流那次读还是空的，无
    // 条件的 `= EXCLUDED` 就会把对方刚写的抹掉。union_id 尤其要命：它是跨飞书应用
    // 收敛身份的唯一依据，抹掉之后按 union_id 找人那条路就断了。
    //
    // 方向跟 common_user_id 相反：那条是旧值优先（首写者 canonical），这两条是新值
    // 优先、新值为空才退回旧值。空串和 NULL 都算"没有值"，所以要 NULLIF。
    it('冲突子句不会用空值盖掉另一条流刚写进去的 union_id / name', async () => {
        h.reply([{ common_user_id: 'winner' }]);

        await h.store.claimCommonUserId(
            { app_id: 'cli_a', open_id: 'ou_1' },
            // 并发那一刻本流手上就是这个：union_id 没读到，名字兜底成了空串
            { union_id: undefined, name: '' },
            'my_candidate',
        );

        const insert = h.sqlOf('INSERT INTO "lark_user_open_id"');
        expect(insert.sql).toContain(
            '"union_id" = COALESCE(NULLIF(EXCLUDED."union_id", \'\'), ' +
                '"lark_user_open_id"."union_id")',
        );
        expect(insert.sql).toContain(
            '"name" = COALESCE(NULLIF(EXCLUDED."name", \'\'), "lark_user_open_id"."name")',
        );
        // 这两句同样用表名限定引用旧行，同样只在 INSERT 不带 alias 时合法
        expect(insert.sql).not.toContain(' AS "');
    });

    it('lark_base_chat_info 同理，且不覆盖已有的 chat_mode', async () => {
        h.reply([{ common_conversation_id: 'winner' }]);

        const canonical = await h.store.claimCommonConversationId(
            { chat_id: 'oc_1', chat_mode: 'p2p' },
            'my_candidate',
        );

        expect(canonical).toBe('winner');
        const insert = h.sqlOf('INSERT INTO "lark_base_chat_info"');
        expect(insert.sql).toContain('ON CONFLICT ("chat_id") DO UPDATE');
        expect(insert.sql).toContain(
            '"common_conversation_id" = COALESCE("lark_base_chat_info".' +
                '"common_conversation_id", EXCLUDED."common_conversation_id")',
        );
        // 别的代码路径建的行知道得比我们从 scope 推的更准，不许改
        expect(insert.sql).not.toContain('"chat_mode" = EXCLUDED');
        expect(insert.sql).toContain('RETURNING common_conversation_id');
        expect(insert.params).toEqual(['oc_1', 'p2p', 'my_candidate']);
        // 同上：表名限定只在无 alias 时合法。
        expect(insert.sql).not.toContain(' AS "');
    });

    it('收敛到别的应用下已有的身份时，只改 common_user_id 这一列', async () => {
        await h.store.linkLarkUser({ app_id: 'cli_a', open_id: 'ou_1' }, 'cu_canonical');

        const update = h.sqlOf('UPDATE "lark_user_open_id"');
        expect(update.sql).toContain('"common_user_id" =');
        expect(update.sql).not.toContain('"name" =');
        expect(update.sql).not.toContain('"union_id" =');
        expect(update.params).toEqual(['cu_canonical', 'cli_a', 'ou_1']);
    });

    // 重放的地基：同一条消息第二次进来时这条语句必须是静默的 no-op。
    it('common_message 是 insert-or-ignore', async () => {
        await h.store.insertCommonMessage({
            common_message_id: 'cm_1',
            channel: 'lark',
            common_conversation_id: 'cc_1',
            common_user_id: 'cu_1',
            sender_display_name: '张三',
            role: 'user',
            content: [{ kind: 'text', text: 'hi' }],
            content_text: 'hi',
            common_root_message_id: 'cm_1',
            common_reply_message_id: undefined,
            scope: 'group',
            message_type: 'text',
            bot_name: 'chiwei',
            event_time: '1700000000000',
        });

        const insert = h.sqlOf('INSERT INTO "common_message"');
        expect(insert.sql).toContain('ON CONFLICT DO NOTHING');
        expect(insert.params).toEqual([
            'cm_1',
            'lark',
            'cc_1',
            'cu_1',
            '张三',
            'user',
            '[{"kind":"text","text":"hi"}]',
            'hi',
            'cm_1',
            'group',
            'text',
            'chiwei',
            '1700000000000',
        ]);
    });

    // 反过来：lark_message 撞主键必须炸。忽略掉就等于让一条飞书消息映射到两个
    // 公共层身份，而且没有任何信号。
    it('lark_message 是普通 insert，撞主键要炸', async () => {
        await h.store.insertLarkMessage({
            om_id: 'om_1',
            common_message_id: 'cm_1',
            chat_id: 'oc_1',
            sender_open_id: 'ou_1',
            sender_union_id: 'on_1',
            root_om_id: 'om_0',
            reply_om_id: undefined,
            message_type: 'text',
            raw_event: { message: { message_id: 'om_1' } },
        });

        const insert = h.sqlOf('INSERT INTO "lark_message"');
        expect(insert.sql).not.toContain('ON CONFLICT');
        expect(insert.params).toEqual([
            'om_1',
            'cm_1',
            'oc_1',
            'ou_1',
            'on_1',
            'om_0',
            'text',
            '{"message":{"message_id":"om_1"}}',
        ]);
    });

    it('common_bot_presence 按 (common_conversation_id, bot_name) upsert', async () => {
        await h.store.markBotPresent('cc_1', 'chiwei', true);

        const insert = h.sqlOf('INSERT INTO "common_bot_presence"');
        expect(insert.sql).toContain('ON CONFLICT ( "common_conversation_id", "bot_name" )');
        expect(insert.params?.slice(0, 3)).toEqual(['cc_1', 'chiwei', true]);
    });
});

describe('事务', () => {
    it('atomically 里的写入跑在同一个 BEGIN/COMMIT 之间', async () => {
        await h.store.atomically(async (tx) => {
            await tx.insertCommonMessage({
                common_message_id: 'cm_1',
                channel: 'lark',
                common_conversation_id: 'cc_1',
                common_user_id: 'cu_1',
                role: 'user',
                content: [],
                common_root_message_id: 'cm_1',
                scope: 'group',
                message_type: 'text',
                bot_name: 'chiwei',
                event_time: '1',
            });
            await tx.insertLarkMessage({
                om_id: 'om_1',
                common_message_id: 'cm_1',
                chat_id: 'oc_1',
                message_type: 'text',
            });
        });

        expect(h.statements()).toEqual([
            'begin',
            'INSERT INTO "common_message"("common_message_id",',
            'INSERT INTO "lark_message"("om_id",',
            'commit',
        ]);
    });

    it('里面抛错就回滚，错误照样往外抛', async () => {
        await expect(
            h.store.atomically(async (tx) => {
                await tx.insertCommonMessage({
                    common_message_id: 'cm_1',
                    channel: 'lark',
                    common_conversation_id: 'cc_1',
                    common_user_id: 'cu_1',
                    role: 'user',
                    content: [],
                    common_root_message_id: 'cm_1',
                    scope: 'group',
                    message_type: 'text',
                    bot_name: 'chiwei',
                    event_time: '1',
                });
                throw new Error('lark_message insert failed');
            }),
        ).rejects.toThrow('lark_message insert failed');

        expect(h.statements()).toEqual([
            'begin',
            'INSERT INTO "common_message"("common_message_id",',
            'rollback',
        ]);
    });

    it('事务里的读也走同一条连接', async () => {
        h.reply([]);
        await h.store.atomically(async (tx) => {
            await tx.larkMessage('om_1');
        });

        expect(h.statements()).toEqual([
            'begin',
            'SELECT "LarkMessage"."om_id" AS',
            'commit',
        ]);
    });
});
