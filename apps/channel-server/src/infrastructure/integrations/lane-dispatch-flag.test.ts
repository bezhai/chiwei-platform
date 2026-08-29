// 「是否按泳道绑定分流入站消息」flag 的 key 契约。
//
// 开关的取值语义（缺失 / false / 'true' 各算什么）由 @inner/shared 的 DynamicConfig
// 负责，那边有自己的测试；这里只钉住两个渠道读的是同一个 key。

import { describe, it, expect } from 'bun:test';
import { INBOUND_LANE_DISPATCH_FLAG } from './lane-dispatch-flag';

// ⚠️ 跨服务契约：lark-service 的 lark/ingress/lane-handoff.ts 用的是同名 key（那边有
// 一条同样写死字面量的断言）。改名只改一边的症状是一个渠道分流、另一个不分流，而且
// 不报错。
describe('INBOUND_LANE_DISPATCH_FLAG', () => {
    it('is the same dynamic config key lark-service reads', () => {
        expect(INBOUND_LANE_DISPATCH_FLAG).toBe('enable_inbound_lane_dispatch');
    });
});
