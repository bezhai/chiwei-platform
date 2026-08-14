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

// ---- inbound_lane 按 channel 分区的切换开关 ----
//
// 换队列名不可能原子发布：生产者和消费者在不同的 Deployment 里。所以是两个 key、两个
// 动作，顺序是「消费侧先订上分区队列 → 再切生产者」。合成一个的话，翻开关那一刻新队列
// 还没有消费者，消息只能干堆着。
//
// **两个 key 在 prod 都还没建**（= 默认关）：这场迁移只上了代码，没上过线，泳道信封
// 目前全部走共享队列 inbound_lane.{lane}。飞书拆服务用的是另一套（所有权收窄 +
// 共享队列上的认领判断），跟这两个开关无关。
//
// 两个 key 都与 lark-service 用的同名：切换期间两个服务必须同进同出，不然会出现一边
// 投新队列、一边投旧队列的分裂。
//
// 走 dynamic config 而不是 env：Release env 会被部署的 POST 清空，长期开关放在那里会
// 在某次部署之后悄悄失效。

// 消费侧：除共享队列外，是否再订阅 inbound_lane.{channel}.{lane}。
// 订阅是启动动作，只在起消费者时读一次——翻开之后要重启消费者才生效。启动时没有请求
// 上下文，所以按 prod 解析：这是一个全局切换开关，给某条泳道单独配不会生效。
export const INBOUND_LANE_CHANNEL_CONSUME_FLAG = 'enable_inbound_lane_channel_consume';

export async function isInboundLaneChannelConsumeEnabled(): Promise<boolean> {
    return dynamicConfig.getBool(INBOUND_LANE_CHANNEL_CONSUME_FLAG, false);
}

// 生产侧：交接的信封投分区队列还是分区前的共享队列。每次投递现读（10s 缓存）。
export const INBOUND_LANE_CHANNEL_PUBLISH_FLAG = 'enable_inbound_lane_channel_publish';

export async function isInboundLaneChannelPublishEnabled(): Promise<boolean> {
    return dynamicConfig.getBool(INBOUND_LANE_CHANNEL_PUBLISH_FLAG, false);
}
