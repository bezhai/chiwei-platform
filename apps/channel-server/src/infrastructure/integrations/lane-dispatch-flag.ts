// 「是否按泳道绑定分流入站消息」的动态开关。
//
// 默认 off = 完全现状行为（prod 自己处理，不算 lane、不交接）。只有 dynamic config 里
// enable_inbound_lane_dispatch 显式为 true 才开启；缺失 / false / 读取失败一律 off。
//
// 走 @inner/shared 的 DynamicConfig（10s 缓存、按 context lane 解析、读取失败 fallback
// 到 defaultValue），与项目「业务行为参数走 dynamic config」的口径一致；default=false 是
// 零回归红线（读不到 / 未配置一律按现状不分流）。
//
// ⚠️ **跨服务契约**：lark-service 的 lark/ingress/lane-handoff.ts 用的是同一个 key
// （那边有一条同样写死字面量的断言）。两个渠道必须同进同出，不然会出现一边分流、一边
// 不分流的分裂。key 名里的 `inbound_lane` 是历史 —— 它当年指的是那条队列，现在指的是
// 「入站消息按泳道分流」这件事本身，改名要动 prod 上已经配好的值，不值当。
//
// 这个开关做的是**交接给谁**，与开关是不是打开无关的那半条链（接收端点）永远挂着：
// 泳道的 Service 不存在时 sidecar 会把交接打回 prod 自己，那时接住它的就是 prod 上的
// 同一条端点。

import { DynamicConfig } from '@inner/shared';

export const INBOUND_LANE_DISPATCH_FLAG = 'enable_inbound_lane_dispatch';

const dynamicConfig = new DynamicConfig();

// 生产入口：走 dynamic config 单例，default=false（零回归）。
export async function isInboundLaneDispatchEnabled(): Promise<boolean> {
    return dynamicConfig.getBool(INBOUND_LANE_DISPATCH_FLAG, false);
}
