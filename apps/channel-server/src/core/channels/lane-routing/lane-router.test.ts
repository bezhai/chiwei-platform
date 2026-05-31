import { describe, it, expect } from 'bun:test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { LaneRouter, type LaneRoutingStore } from './lane-router';

// LaneRouter 是平台无关的泳道决策能力（本期只做 bot 维度）。它的入参只认统一概念
// （channel + 全局 bot 标识），绝不接触飞书原始字段。读取靠注入的结构型
// LaneRoutingStore（ORM-free，生产由 TypeORM 实现注入），所以本测试用内存 fake
// 顶替真实 DB，纯跑、不连库。
//
// 这个 fake 忠实模拟真实 lane_routing 表的查询语义：按「全局 bot 标识」查命中的
// lane_name；没绑定返回 null。LaneRouter 据此决定 lane：命中返回 lane_name，
// 未命中返回 'prod'（本期决策优先级就两档：bot 命中 > prod 默认）。

// 一个可计数、可重设绑定的内存 store，记录被查了几次，用来验证缓存命中不打 DB。
class FakeLaneRoutingStore implements LaneRoutingStore {
    findBotLaneCalls = 0;
    private bindings = new Map<string, string>();

    setBotBinding(botGlobalId: string, lane: string): void {
        this.bindings.set(botGlobalId, lane);
    }

    removeBotBinding(botGlobalId: string): void {
        this.bindings.delete(botGlobalId);
    }

    async findBotLane(botGlobalId: string): Promise<string | null> {
        this.findBotLaneCalls += 1;
        return this.bindings.get(botGlobalId) ?? null;
    }
}

const NOW = 1_700_000_000_000;

describe('LaneRouter.resolveLane — 平台无关的 bot 维度泳道决策', () => {
    it('bot 命中绑定：返回绑定的 lane_name', async () => {
        const store = new FakeLaneRoutingStore();
        store.setBotBinding('赤尾', 'ppe-foo');
        const router = new LaneRouter(store);

        const lane = await router.resolveLane('lark', '赤尾');
        expect(lane).toBe('ppe-foo');
    });

    it('bot 未命中绑定：默认回 prod（不返回 null、不抛错）', async () => {
        const store = new FakeLaneRoutingStore();
        const router = new LaneRouter(store);

        const lane = await router.resolveLane('lark', '没绑定的bot');
        expect(lane).toBe('prod');
    });

    it('显式绑定到 prod 也返回 prod（命中即返回，prod 不是特殊值）', async () => {
        const store = new FakeLaneRoutingStore();
        store.setBotBinding('赤尾', 'prod');
        const router = new LaneRouter(store);

        const lane = await router.resolveLane('lark', '赤尾');
        expect(lane).toBe('prod');
    });

    it('30s TTL 内重复决策走缓存：同一 bot 不重复查 DB', async () => {
        const store = new FakeLaneRoutingStore();
        store.setBotBinding('赤尾', 'ppe-foo');
        let clock = NOW;
        const router = new LaneRouter(store, () => clock);

        expect(await router.resolveLane('lark', '赤尾')).toBe('ppe-foo');
        expect(await router.resolveLane('lark', '赤尾')).toBe('ppe-foo');
        // TTL 内推进时间但不超过 30s：仍走缓存。
        clock = NOW + 29_999;
        expect(await router.resolveLane('lark', '赤尾')).toBe('ppe-foo');

        expect(store.findBotLaneCalls).toBe(1);
    });

    it('未命中（prod 默认）同样进缓存，避免每条消息为未绑定 bot 反复打 DB', async () => {
        const store = new FakeLaneRoutingStore();
        const router = new LaneRouter(store);

        expect(await router.resolveLane('lark', '没绑定')).toBe('prod');
        expect(await router.resolveLane('lark', '没绑定')).toBe('prod');
        expect(store.findBotLaneCalls).toBe(1);
    });

    it('TTL 过期后重新查 DB（缓存条目按 30s 失效）', async () => {
        const store = new FakeLaneRoutingStore();
        store.setBotBinding('赤尾', 'ppe-foo');
        let clock = NOW;
        const router = new LaneRouter(store, () => clock);

        expect(await router.resolveLane('lark', '赤尾')).toBe('ppe-foo');
        expect(store.findBotLaneCalls).toBe(1);
        // 跨过 30s TTL：缓存失效，重新查 DB。
        clock = NOW + 30_001;
        expect(await router.resolveLane('lark', '赤尾')).toBe('ppe-foo');
        expect(store.findBotLaneCalls).toBe(2);
    });

    it('不同 channel 的同名 bot 是不同缓存条目（缓存 key 含 channel，不串）', async () => {
        const store = new FakeLaneRoutingStore();
        store.setBotBinding('赤尾', 'ppe-foo');
        const router = new LaneRouter(store);

        expect(await router.resolveLane('lark', '赤尾')).toBe('ppe-foo');
        // store 只按 botGlobalId 查（本期 route_type=bot 单维度），另一个 channel
        // 的同名 bot 应触发一次新的 DB 查询，而不是命中 lark 的缓存。
        await router.resolveLane('qq', '赤尾');
        expect(store.findBotLaneCalls).toBe(2);
    });

    it('clearCache 后重新查 DB（admin 改绑定后主动失效，不必等 30s）', async () => {
        const store = new FakeLaneRoutingStore();
        store.setBotBinding('赤尾', 'ppe-foo');
        const router = new LaneRouter(store);

        expect(await router.resolveLane('lark', '赤尾')).toBe('ppe-foo');
        expect(store.findBotLaneCalls).toBe(1);

        // 模拟 admin 改绑定：换 lane 后主动 clearCache，下一次决策必须看到新值。
        store.setBotBinding('赤尾', 'ppe-bar');
        router.clearCache();

        expect(await router.resolveLane('lark', '赤尾')).toBe('ppe-bar');
        expect(store.findBotLaneCalls).toBe(2);
    });

    it('clearCache 后解绑也立即生效：原绑定移除后回到 prod 默认', async () => {
        const store = new FakeLaneRoutingStore();
        store.setBotBinding('赤尾', 'ppe-foo');
        const router = new LaneRouter(store);

        expect(await router.resolveLane('lark', '赤尾')).toBe('ppe-foo');

        store.removeBotBinding('赤尾');
        router.clearCache();

        expect(await router.resolveLane('lark', '赤尾')).toBe('prod');
    });
});

describe('LaneRouter 平台无关红线 — 决策能力源码不含任何飞书字段名', () => {
    it('lane-router.ts 不出现 chat_id/open_id/event/sender 等飞书字段名', () => {
        const src = readFileSync(join(import.meta.dir, 'lane-router.ts'), 'utf8');
        // 这些是飞书消息体里的字段名；决策只认 channel + 全局 bot 标识，源码里
        // 一旦出现它们就说明决策又退回去碰平台原始字段了（设计 §3.2 红线）。
        const FORBIDDEN_LARK_FIELDS = [
            'chat_id',
            'open_id',
            'open_chat_id',
            'sender',
            'event_type',
            // 'event' 作为独立单词（避免误伤 EventEmitter 之类，这里按 \bevent\b）
            /\bevent\b/,
        ];
        for (const f of FORBIDDEN_LARK_FIELDS) {
            const hit =
                typeof f === 'string' ? src.includes(f) : f.test(src);
            expect(hit).toBe(false);
        }
    });
});
