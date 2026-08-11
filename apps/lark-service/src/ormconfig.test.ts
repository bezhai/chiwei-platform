import { beforeAll, describe, expect, it } from 'bun:test';
import { DataSource } from 'typeorm';
import { boundDataSource } from '@inner/shared/persistence';

import { LARK_SERVICE_ENTITIES, larkDataSource } from './ormconfig';

// 这个 DataSource 的实体清单是本服务能读写哪些表的唯一事实来源。漏一条的症状是
// 运行期 "No metadata found"；多一条更隐蔽 —— 拆服务的全部意义就是飞书进程不再
// 持有 QQ 的表和凭据，多注册一张 qq_* 表等于隔离没做。
//
// 只数数组长度挡不住真正的坑：同一张表被两个 class 定义（cutover 窗口里
// channel-server 还留着一份 lark_* 实体，最容易撞的就是这个）长度照样对，但
// getRepository 拿的是另一个 constructor，运行期直接 No metadata。所以这里**真的
// 让 TypeORM 解析一遍 metadata**，只是不建连接。

type MetadataBuildable = DataSource & { buildMetadatas(): Promise<void> };

// 本服务应当持有的全部表。新增/移除表必须同步改这里 —— 这份清单就是评审时
// 「lark-service 碰哪些表」的答案。
const EXPECTED_TABLES = [
    // 渠道无关的公共层（定义在 @inner/shared/entities，由本服务选择注册）
    'bot_config',
    'common_agent_response',
    'common_bot_presence',
    'common_conversation',
    'common_message',
    'common_user',
    'lane_routing',
    'user_blacklist',
    // 飞书独占
    'lark_base_chat_info',
    'lark_emoji',
    'lark_group_chat_info',
    'lark_group_member',
    'lark_message',
    'lark_user',
    'lark_user_open_id',
    // 表名里没有 lark 前缀，但只被 /bind、/unbind 和退群自动拉回读写。
    'user_group_binding',
].sort();

let probe: DataSource;

beforeAll(async () => {
    probe = new DataSource({ ...larkDataSource().options });
    await (probe as MetadataBuildable).buildMetadatas();
});

describe('lark-service entity registration', () => {
    it('builds real TypeORM metadata for every registered entity (no connection)', () => {
        expect(probe.entityMetadatas).toHaveLength(LARK_SERVICE_ENTITIES.length);
    });

    it('registers exactly the expected table set (by name, not by count)', () => {
        expect(probe.entityMetadatas.map((m) => m.tableName).sort()).toEqual(EXPECTED_TABLES);
    });

    it('holds no other channel tables', () => {
        const foreign = probe.entityMetadatas
            .map((m) => m.tableName)
            .filter((t) => t.startsWith('qq_'));
        expect(foreign).toEqual([]);
    });

    it('every registered entity resolves by constructor identity', () => {
        for (const entity of LARK_SERVICE_ENTITIES) {
            expect(() => probe.getMetadata(entity)).not.toThrow();
        }
    });

    it('no table is defined by two different entity classes', () => {
        const byTable = new Map<string, string[]>();
        for (const meta of probe.entityMetadatas) {
            byTable.set(meta.tableName, [...(byTable.get(meta.tableName) ?? []), meta.name]);
        }
        expect([...byTable.entries()].filter(([, names]) => names.length > 1)).toEqual([]);
    });

    it('every entity has a primary column (metadata actually resolved, not a stub)', () => {
        for (const meta of probe.entityMetadatas) {
            expect(meta.primaryColumns.length).toBeGreaterThan(0);
        }
    });
});

// 共享包里的通用能力（bot 身份目录 / 黑名单规则 / chat.request 的 pending 行落库 /
// 泳道绑定解析）读写的是**本服务组装的这个 DataSource**，靠 bindDataSource 递进去。
// 漏了这一步单测照样全绿（没人碰真库），生产上第一条消息进 runRules 才炸。
describe('lark-service binds its DataSource for shared capabilities', () => {
    it('binds this service DataSource into the shared package', () => {
        expect(boundDataSource()).toBe(larkDataSource());
    });

    it('hands out one DataSource, not one per call', () => {
        expect(larkDataSource()).toBe(larkDataSource());
    });

    it('registers every table the shared capabilities read/write', () => {
        const tables = probe.entityMetadatas.map((m) => m.tableName);
        // BotDirectory: bot_config + common_user
        expect(tables).toContain('bot_config');
        expect(tables).toContain('common_user');
        // NotBlocked 规则
        expect(tables).toContain('user_blacklist');
        // chat.request 的 pending 行
        expect(tables).toContain('common_agent_response');
        // 投影链路 + 在场状态
        expect(tables).toContain('common_message');
        expect(tables).toContain('common_conversation');
        expect(tables).toContain('common_bot_presence');
        // 泳道绑定解析
        expect(tables).toContain('lane_routing');
    });
});
