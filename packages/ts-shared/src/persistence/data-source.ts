// PostgreSQL 连接的构造与进程级绑定。
//
// 共享包提供的是「怎么连」，不是「连上之后有哪些表」。实体清单整份由调用方
// 传入：每个服务传公共层 + 自己独占的那几张表。包内一条都不追加 —— 否则每个
// 服务会连带加载别的服务独占的表，正是拆服务要消灭的耦合。
//
// 绑定（bindDataSource）解决的是另一个问题：包内的通用能力（bot 身份目录、
// 黑名单规则、chat.request 的 pending 行落库）需要读写库，但它们不能 import
// 任何服务的 ormconfig —— 那是反向依赖。所以由各服务的组装根在启动期把自己
// 构造好的 DataSource 绑进来，包内代码只从绑定处读。

import { DataSource, type EntityTarget, type ObjectLiteral, type Repository } from 'typeorm';

export interface PostgresConnectionSettings {
    host: string;
    port: number;
    username: string;
    password: string;
    database: string;
}

export interface PostgresDataSourceInput {
    // 本服务要注册的实体全集。包内不追加任何一条 —— 想用 common_* 就自己
    // 从 '@inner/shared/entities' import 进来放进这个数组。
    entities: Function[];
    // 不传则整份取 POSTGRES_* 环境变量。测试或多库场景可逐项覆盖。
    connection?: Partial<PostgresConnectionSettings>;
}

function settingsFromEnv(): PostgresConnectionSettings {
    return {
        host: process.env.POSTGRES_HOST!,
        port: Number(process.env.POSTGRES_PORT) || 5432,
        username: process.env.POSTGRES_USER!,
        password: process.env.POSTGRES_PASSWORD!,
        database: process.env.POSTGRES_DB!,
    };
}

export function createPostgresDataSource(input: PostgresDataSourceInput): DataSource {
    const connection = { ...settingsFromEnv(), ...input.connection };
    return new DataSource({
        type: 'postgres',
        ...connection,
        // 禁止 ORM 在启动时 sync schema；DDL 走 /ops-db submit 或 migration。
        synchronize: false,
        logging: ['error', 'schema', 'warn'],
        entities: input.entities,
    });
}

// ---- 进程级绑定 ----

let bound: DataSource | undefined;

/**
 * 组装根在启动期调用一次。重复绑定**不同**实例 fail-closed 抛错：两个
 * DataSource 同时在跑意味着两套连接池、两份实体元数据，出问题时症状是
 * 「同一张表有时查得到有时查不到」，必须在启动期炸而不是运行期猜。
 * 同一实例重复绑定是幂等的（模块图里被多次求值时不该炸）。
 */
export function bindDataSource(dataSource: DataSource): void {
    if (bound && bound !== dataSource) {
        throw new Error(
            'a DataSource is already bound to this process; ' +
                'binding a second one would run two connection pools side by side',
        );
    }
    bound = dataSource;
}

export function boundDataSource(): DataSource {
    if (!bound) {
        throw new Error(
            'no DataSource is bound to this process; ' +
                'the service composition root must call bindDataSource() before ' +
                'any shared repository is used',
        );
    }
    return bound;
}

/** 测试钩子：解绑，避免跨用例污染。 */
export function resetBoundDataSource(): void {
    bound = undefined;
}

/** 绑定 DataSource 上的仓储取用口，惰性求值 —— import 期不碰连接。 */
export function repositoryFor<T extends ObjectLiteral>(entity: EntityTarget<T>): Repository<T> {
    return boundDataSource().getRepository(entity);
}
