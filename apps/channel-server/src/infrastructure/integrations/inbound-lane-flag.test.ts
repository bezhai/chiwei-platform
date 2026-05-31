// 「是否启用处理层分流」flag 纯函数单测（§3 / Task 10）。
// 默认 off：缺失 / falsy 一律 false（零回归）。只有显式 true / 'true' 才 on。

import { describe, it, expect } from 'bun:test';
import { readInboundLaneDispatchFlag } from './inbound-lane-flag';

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
