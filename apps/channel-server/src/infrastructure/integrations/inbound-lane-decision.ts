// 入站分流决策（lane-routing-redesign §3/§4.2）。把决策点的分叉逻辑抽成纯函数：
//
//   flag off                  → local（完全旁路，零回归：不算 lane、不发 MQ）
//   flag on + lane==本进程lane → local（prod 消息本地处理，绝不投 inbound_lane.prod）
//   flag on + lane!=本进程lane → dispatch（投 inbound_lane.{lane}，本地不再处理）
//
// resolveLane 由调用方注入（生产=getLaneRouter().resolveLane），决策只看平台无关
// 的 channel + common conversation + 全局 bot 标识（§3.2 平台无关红线）。

export interface InboundDispatchInput {
    // 动态 flag「是否启用处理层分流」（§3 / Task 10）。默认 off = 现状行为。
    flagEnabled: boolean;
    // 本进程所属 lane（prod channel-server = 'prod'）。
    currentLane: string;
    channel: string;
    botGlobalId: string;
    commonConversationId?: string;
    // 平台无关的 lane 决策（注入，便于测试 + 解耦 ORM）。
    resolveLane: (
        channel: string,
        botGlobalId: string,
        commonConversationId: string | undefined,
    ) => Promise<string>;
}

export interface InboundDispatchDecision {
    // local = 本进程继续走入站后半段；dispatch = 投 inbound_lane.{lane}。
    action: 'local' | 'dispatch';
    lane: string;
}

export async function resolveInboundDispatch(
    input: InboundDispatchInput,
): Promise<InboundDispatchDecision> {
    // flag off：完全旁路。不调 resolveLane（零回归 + 不打 DB），按本进程 lane 本地处理。
    if (!input.flagEnabled) {
        return { action: 'local', lane: input.currentLane };
    }

    const lane = await input.resolveLane(
        input.channel,
        input.botGlobalId,
        input.commonConversationId,
    );

    // lane == 本进程 lane（含 prod 占绝大多数）：本地处理，绝不投 MQ（§4.2 prod 不发
    // MQ；也防把消息投给自己造成双跑）。
    if (lane === input.currentLane) {
        return { action: 'local', lane };
    }

    // lane != 本进程 lane：投 inbound_lane.{lane}，本进程到此为止。
    return { action: 'dispatch', lane };
}
