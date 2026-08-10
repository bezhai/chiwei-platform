// 花名册真身发出去的到底是什么 SQL。
//
// "谁能被 @" 的判断由 mentions.test.ts 用手写替身验；那份逻辑再正确也说明不了真身
// 有没有查错表、漏掉 join、或者忘了把 is_leave 选出来 —— 漏掉最后这个的症状是
// **所有人的 hasLeft 都是 undefined，退群的人照样能被 @**。
//
// 开发机连不到数据库，所以用真的 TypeORM + 真的实体元数据建一个不连库的 DataSource，
// 把 query runner 换成录音机。这样拿到的不是"我以为它会生成什么"，是它真的生成了什么。

import { describe, expect, it } from 'bun:test';
import { DataSource } from 'typeorm';
import { Broadcaster } from 'typeorm/subscriber/Broadcaster.js';

import { LARK_SERVICE_ENTITIES } from '../../ormconfig';
import { postgresLarkGroupRoster } from './postgres-roster';
import type { LarkGroupRoster } from './mentions';

async function harness(rows: Array<Record<string, unknown>>): Promise<{
    roster: LarkGroupRoster;
    sql(): string;
    params(): unknown[];
}> {
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
    // 只解析元数据，不连库 —— initialize() 建连接之前做的正是这一步。
    // **必须 await**：buildMetadatas 是异步的，不等它就拿到一个空的元数据表，
    // 症状是 "No metadata for X was found"。
    await (dataSource as unknown as { buildMetadatas(): Promise<void> }).buildMetadatas();
    (dataSource as unknown as { isInitialized: boolean }).isInitialized = true;

    const recorded: Array<{ sql: string; params: unknown[] }> = [];
    const runner: Record<string, unknown> = {
        connection: dataSource,
        isReleased: false,
        isTransactionActive: false,
        data: {},
        connect: async () => {},
        release: async () => {},
        // getRawMany 走 useStructuredResult=true 这条路，要的是信封而不是裸数组。
        query: async (sql: string, params: unknown[], structured?: boolean) => {
            recorded.push({ sql, params });
            return structured ? { records: rows, affected: rows.length, raw: rows } : rows;
        },
    };
    runner.broadcaster = new Broadcaster(runner as never);
    runner.manager = dataSource.createEntityManager(runner as never);
    dataSource.createQueryRunner = () => runner as never;

    return {
        roster: postgresLarkGroupRoster(dataSource),
        sql: () => recorded[0]?.sql ?? '',
        params: () => recorded[0]?.params ?? [],
    };
}

describe('postgresLarkGroupRoster', () => {
    it('读 lark_group_member，名字从 lark_user join 出来，按 chat 过滤', async () => {
        const h = await harness([]);
        await h.roster.entries('oc_group');

        expect(h.sql()).toContain('"lark_group_member"');
        expect(h.sql()).toContain('"lark_user"');
        expect(h.sql()).toContain('"chat_id" = $1');
        expect(h.params()).toEqual(['oc_group']);
    });

    it('把行翻成端口的口径：union id / 名字 / 在不在群里', async () => {
        const h = await harness([
            { union_id: 'on_1', name: '张三', is_leave: false },
            { union_id: 'on_2', name: '李四', is_leave: true },
        ]);

        expect(await h.roster.entries('oc_group')).toEqual([
            { unionId: 'on_1', name: '张三', hasLeft: false },
            { unionId: 'on_2', name: '李四', hasLeft: true },
        ]);
    });

    it('is_leave 是 null 的老行按"还在群里"算', async () => {
        // 这一列有默认值 false，但老数据里存在 null。null 当成"退群了"会让整群人
        // 突然 @ 不动。
        const h = await harness([{ union_id: 'on_1', name: '张三', is_leave: null }]);
        expect(await h.roster.entries('oc_group')).toEqual([
            { unionId: 'on_1', name: '张三', hasLeft: false },
        ]);
    });

    it('退群的人也读出来，不在 SQL 里过滤', async () => {
        // 过滤是判断，判断归 mentions.ts。真身在这里少读一行，mentions 那边的
        // hasLeft 分支就永远走不到，测试也就守不住任何东西。
        const h = await harness([]);
        await h.roster.entries('oc_group');
        expect(h.sql()).not.toContain('is_leave" = ');
    });
});
