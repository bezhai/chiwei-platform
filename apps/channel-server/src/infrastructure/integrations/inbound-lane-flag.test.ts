// 「是否启用处理层分流」flag 纯函数单测（§3 / Task 10）。
// 默认 off：缺失 / falsy 一律 false（零回归）。只有显式 true / 'true' 才 on。

import { describe, it, expect } from 'bun:test';
import {
    INBOUND_LANE_CHANNEL_CONSUME_FLAG,
    INBOUND_LANE_CHANNEL_PUBLISH_FLAG,
    readInboundLaneDispatchFlag,
} from './inbound-lane-flag';

// ⚠️ 跨服务契约：lark-service 用的是同名 key（lane-queue.ts / lane-handoff.ts 里各有
// 一条同样写死字面量的断言）。改名只改一边的症状是切换期间一个服务投新队列、另一个
// 还投旧队列——两条路各跑一半，而且不报错。
describe('inbound_lane 按 channel 分区的切换开关', () => {
    it('消费侧和生产侧是两个 key，不能合成一个', () => {
        expect(INBOUND_LANE_CHANNEL_CONSUME_FLAG).toBe('enable_inbound_lane_channel_consume');
        expect(INBOUND_LANE_CHANNEL_PUBLISH_FLAG).toBe('enable_inbound_lane_channel_publish');
        expect(INBOUND_LANE_CHANNEL_CONSUME_FLAG).not.toBe(INBOUND_LANE_CHANNEL_PUBLISH_FLAG);
    });
});

describe('readInboundLaneDispatchFlag（处理层分流开关）', () => {
    it('key 缺失 → off', () => {
        expect(readInboundLaneDispatchFlag({})).toBe(false);
    });
    it('值为 false → off', () => {
        expect(readInboundLaneDispatchFlag({ enable_inbound_lane_dispatch: false })).toBe(false);
    });
    it('值为 true → on', () => {
        expect(readInboundLaneDispatchFlag({ enable_inbound_lane_dispatch: true })).toBe(true);
    });
    it('值为字符串 "true" → on', () => {
        expect(readInboundLaneDispatchFlag({ enable_inbound_lane_dispatch: 'true' })).toBe(true);
    });
    it('值为字符串 "false" → off', () => {
        expect(readInboundLaneDispatchFlag({ enable_inbound_lane_dispatch: 'false' })).toBe(false);
    });
    it('值为其他真值字符串 "1" → off（只认 true，避免误开）', () => {
        expect(readInboundLaneDispatchFlag({ enable_inbound_lane_dispatch: '1' })).toBe(false);
    });
});
