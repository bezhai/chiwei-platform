import { describe, it, expect, beforeAll } from 'bun:test';
import { DataSource } from 'typeorm';
import AppDataSource from './ormconfig';
import { boundDataSource } from '@inner/shared/persistence';

// 这个 DataSource 的实体清单是本服务能读写哪些表的唯一事实来源，漏一条的症状是
// 运行期 "No metadata found"（QQ 链路首投影崩过一次就是因为实体 import 了但没进
// entities 数组）。
//
// 只数数组长度挡不住真正的坑：同一张表被两个 class 定义（拆共享包时最容易发生）
// 长度照样对，但 getRepository 拿的是另一个 constructor，运行期直接 No metadata；
// 关系解析失败也只在 TypeORM 真正 build metadata 时才暴露。
//
// 所以这里**真的让 TypeORM 解析一遍 metadata**，只是不建连接：把真实 options 原样
// 喂给一个一次性 DataSource，调 buildMetadatas()（initialize() 里连接之前的那一步）。
// 不碰应用单例，也不需要 PG。

// buildMetadatas 在类型上是 protected（TypeORM 内部由 initialize 调用），运行期公开。
type MetadataBuildable = DataSource & { buildMetadatas(): Promise<void> };

// 本服务应当持有的全部表。新增/移除表必须同步改这里 —— 这份清单就是评审时
// 「这个服务碰哪些表」的答案。
const EXPECTED_TABLES = [
    // 渠道无关的公共层（定义在 @inner/shared/entities）
    'bot_config',
    'common_agent_response',
    'common_bot_presence',
    'common_conversation',
    'common_message',
    'common_user',
    'lane_routing',
    'user_blacklist',
    // 本服务自己的表
    'qq_group_chat_info',
    'qq_message',
    'qq_user_open_id',
].sort();

let probe: DataSource;

beforeAll(async () => {
    probe = new DataSource({ ...AppDataSource.options });
    await (probe as MetadataBuildable).buildMetadatas();
});

describe('ormconfig entity registration', () => {
    it('builds real TypeORM metadata for every registered entity (no connection)', () => {
        const entities = AppDataSource.options.entities as Function[];
        expect(probe.entityMetadatas).toHaveLength(entities.length);
    });

    it('registers exactly the expected table set (by name, not by count)', () => {
        const tables = probe.entityMetadatas.map((m) => m.tableName).sort();
        expect(tables).toEqual(EXPECTED_TABLES);
    });

    it('every registered entity resolves by constructor identity', () => {
        // 这条是拆共享包后的真正守卫：如果某张表在共享包和本服务里各有一份 class，
        // 表名集合仍然对得上，但用其中一个 constructor 去 getMetadata 会直接抛
        // "No metadata for X was found" —— 正是运行期那个报错的提前暴露。
        const entities = AppDataSource.options.entities as Function[];
        for (const entity of entities) {
            expect(() => probe.getMetadata(entity)).not.toThrow();
        }
    });

    it('no table is defined by two different entity classes', () => {
        const byTable = new Map<string, string[]>();
        for (const meta of probe.entityMetadatas) {
            const names = byTable.get(meta.tableName) ?? [];
            names.push(meta.name);
            byTable.set(meta.tableName, names);
        }
        const duplicated = [...byTable.entries()].filter(([, names]) => names.length > 1);
        expect(duplicated).toEqual([]);
    });

    it('every entity has a primary column (metadata actually resolved, not a stub)', () => {
        for (const meta of probe.entityMetadatas) {
            expect(meta.primaryColumns.length).toBeGreaterThan(0);
        }
    });
});

// 共享包里的通用能力（bot 身份目录 / 黑名单规则 / chat.request 的 pending 行落库 /
// 泳道绑定解析）读写的是**本服务组装的这个 DataSource**，靠组装根 bindDataSource
// 递进去。漏了这一步，单测照样全绿（没人碰真库），但生产上第一条消息进 runRules
// 就会炸「no DataSource is bound」。
describe('ormconfig binds the DataSource for shared capabilities', () => {
    it('binds this service DataSource into the shared package', () => {
        expect(boundDataSource()).toBe(AppDataSource);
    });

    it('registers every table the shared capabilities read/write', () => {
        const tables = probe.entityMetadatas.map((m) => m.tableName);
        // BotDirectory: bot_config + common_user
        expect(tables).toContain('bot_config');
        expect(tables).toContain('common_user');
        // NotBlocked: user_blacklist
        expect(tables).toContain('user_blacklist');
        // makeTextReply 的 pending 行
        expect(tables).toContain('common_agent_response');
        // 投影链路 + 在场状态
        expect(tables).toContain('common_message');
        expect(tables).toContain('common_bot_presence');
        // 泳道绑定解析
        expect(tables).toContain('lane_routing');
    });
});
