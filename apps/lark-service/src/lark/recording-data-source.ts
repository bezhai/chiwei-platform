// 一个**不连库**的 TypeORM DataSource：跑一遍就能拿到它真的会发出去的 SQL。
//
// 只有测试用它。放在 src 下而不是某个 __tests__ 目录，是因为它 import 真的 TypeORM
// 和真的实体元数据 —— 它必须跟生产代码用同一套类型，否则"实体改了列名"这类问题就
// 会绕过它。编译成二进制时它不在任何入口的依赖图里，不进产物。
//
// 为什么需要它：端口的**逻辑**由内存实现验，那份实现再怎么正确，也说明不了真身有没有
// 写错表、漏掉 ON CONFLICT、忘了 ORDER BY。开发机连不到数据库，所以换个办法 ——
// 把 query runner 换成录音机，让真的 TypeORM 生成真的 SQL，然后断言录到的东西。
//
// 拿到的不是"我以为它会生成什么"，是 TypeORM 真的生成了什么。

import { DataSource } from 'typeorm';
import { Broadcaster } from 'typeorm/subscriber/Broadcaster.js';

export interface RecordedStatement {
    sql?: string;
    params?: unknown[];
    tx?: 'begin' | 'commit' | 'rollback';
}

export interface RecordingDataSource {
    dataSource: DataSource;
    recorded: RecordedStatement[];
    /** 下一条 SELECT 返回什么（原始列名，形如 `Alias_column`）。 */
    reply(rows: Array<Record<string, unknown>>): void;
    /** 找出包含某个片段的那条语句。找不到就把录到的全部列出来，省得瞎猜。 */
    sqlOf(fragment: string): RecordedStatement;
    /** 语句序列的粗略形状：事务标记原样，SQL 只留前三个词。 */
    statements(): string[];
}

export function recordingDataSource(entities: Function[]): RecordingDataSource {
    const dataSource = new DataSource({
        type: 'postgres',
        host: 'unused',
        port: 5432,
        username: 'unused',
        password: 'unused',
        database: 'unused',
        synchronize: false,
        entities,
    });
    // 只解析元数据，不连库 —— initialize() 在建连接之前做的正是这一步。
    (dataSource as unknown as { buildMetadatas(): void }).buildMetadatas();
    (dataSource as unknown as { isInitialized: boolean }).isInitialized = true;

    const recorded: RecordedStatement[] = [];
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
        dataSource,
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
