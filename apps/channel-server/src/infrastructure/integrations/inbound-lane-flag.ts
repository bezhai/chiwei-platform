// 「是否启用处理层分流」动态 flag（lane-routing-redesign §3）。
//
// 默认 off = 完全现状行为，保零回归。只有 dynamic config 里
// enable_inbound_lane_dispatch 显式为 true/1/yes 才开启；缺失 / false / 读取失败一律 off。
//
// 走 @inner/shared 的 DynamicConfig（运行时配置 SDK：10s 缓存、按 context lane 解析、
// 读取失败 fallback 到 defaultValue），与项目「业务行为参数走 dynamic config」的
// 既定口径一致；default=false 是零回归红线（读不到 / 未配置一律按现状不分流）。
// channel-server 此前无 dynamic config 消费者，这里建第一个单例（默认 laneProvider
// 读共享 context 的 lane）。

import { DynamicConfig } from '@inner/shared';

export const INBOUND_LANE_DISPATCH_FLAG = 'enable_inbound_lane_dispatch';

const dynamicConfig = new DynamicConfig();

// 纯函数：从一份已取出的配置 map 判断开关。只认显式 true（boolean true 或字符串
// 'true'），其余一律 off。单测直接喂 map 验证「默认 off」语义，不打网络。
export function readInboundLaneDispatchFlag(cfg: Record<string, unknown>): boolean {
    const v = cfg[INBOUND_LANE_DISPATCH_FLAG];
    return v === true || v === 'true';
}

// 生产入口：走 dynamic config 单例，default=false（零回归）。
export async function isInboundLaneDispatchEnabled(): Promise<boolean> {
    return dynamicConfig.getBool(INBOUND_LANE_DISPATCH_FLAG, false);
}
