import { describe, it, expect, beforeEach, afterEach } from 'bun:test';
import type { DataSource } from 'typeorm';

import { LaneRouting } from '../entities/lane-routing';
import { bindDataSource, resetBoundDataSource } from '../persistence/data-source';
import { TypeOrmLaneBindingStore } from './store';

// 钉死 TypeOrmLaneBindingStore 的 wire 契约：lane_routing.route_type 列在真实库里
// 是 character varying（字符串），值是 'bot' / 'chat' / 'group'。生产查询写法必须
// 是 WHERE route_type = 'bot' 字符串。
//
// 这条契约当初被 mock-only 单测漏掉过：上一版把 route_type 误编码成整数枚举
// （enum RouteType { Bot = 1 }，查 route_type = '1'），mock 的 store 测不到这层
// 真实 DB 编码，于是 wire 不匹配漏到运行时——查 route_type='1' 永远命不中真实
// 行（真实值是 'bot'），所有 bot 维度泳道绑定静默失效、全部 fallback 到 prod。
//
// 本测试捕获 findBotLane 实际发给 repository.findOne 的 where 条件，断言
// route_type 用的是字符串字面量 'bot'。任何人若把它改回整数（route_type: 1 / '1'
// / RouteType.Bot），where.route_type 不再 === 'bot'，本测试立即 fail。
//
// 不连真实 PG —— 绑一个假 DataSource 捕获传进 findOne 的 where。顺带钉死 store
// 取的是 LaneRouting 这张实体（拿错实体会静默查到别的表）。

interface CapturedWhere {
    route_type?: unknown;
    route_key?: unknown;
    is_active?: unknown;
}

let capturedWheres: CapturedWhere[] = [];
let capturedEntities: unknown[] = [];
let nextRow: { lane_name: string } | null = null;

function fakeDataSource(): DataSource {
    return {
        getRepository(entity: unknown) {
            capturedEntities.push(entity);
            return {
                async findOne(opts: { where: CapturedWhere }) {
                    capturedWheres.push(opts.where);
                    return nextRow;
                },
            };
        },
    } as unknown as DataSource;
}

beforeEach(() => {
    capturedWheres = [];
    capturedEntities = [];
    nextRow = null;
    resetBoundDataSource();
    bindDataSource(fakeDataSource());
});

afterEach(() => {
    resetBoundDataSource();
});

describe('TypeOrmLaneBindingStore — route_type wire 契约（字符串 chat/bot）', () => {
    it('查询条件 route_type 必须是字符串字面量 bot（不是整数 1 / 字符串 "1"）', async () => {
        nextRow = { lane_name: 'ppe-foo' };

        const store = new TypeOrmLaneBindingStore();
        const lane = await store.findBotLane('赤尾');

        expect(lane).toBe('ppe-foo');
        expect(capturedWheres).toHaveLength(1);

        const where = capturedWheres[0]!;
        // 核心断言：route_type 严格等于字符串 'bot'。把它改回整数枚举
        // （1 / '1' / RouteType.Bot）会让本断言 fail。
        expect(where.route_type).toBe('bot');
        // 严格类型守卫：必须是 string 类型，不是 number。
        expect(typeof where.route_type).toBe('string');
        // route_key 透传全局 bot 标识；is_active 只取生效绑定。
        expect(where.route_key).toBe('赤尾');
        expect(where.is_active).toBe(true);
    });

    it('chat 查询条件 route_type 必须是字符串字面量 chat，route_key 是 common_conversation_id', async () => {
        nextRow = { lane_name: 'ppe-chat' };

        const store = new TypeOrmLaneBindingStore();
        const lane = await store.findChatLane('018f-common-chat');

        expect(lane).toBe('ppe-chat');
        expect(capturedWheres).toHaveLength(1);

        const where = capturedWheres[0]!;
        expect(where.route_type).toBe('chat');
        expect(typeof where.route_type).toBe('string');
        expect(where.route_key).toBe('018f-common-chat');
        expect(where.is_active).toBe(true);
    });

    it('未命中绑定返回 null（findOne 返回 null）', async () => {
        nextRow = null;

        const store = new TypeOrmLaneBindingStore();
        const lane = await store.findBotLane('没绑定的bot');

        expect(lane).toBeNull();
        // 即便未命中，发出的查询仍是字符串 route_type。
        expect(capturedWheres[0]!.route_type).toBe('bot');
    });

    it('查的是 lane_routing 实体（拿错实体会静默查到别的表）', async () => {
        const store = new TypeOrmLaneBindingStore();
        await store.findBotLane('赤尾');
        expect(capturedEntities).toEqual([LaneRouting]);
    });
});
