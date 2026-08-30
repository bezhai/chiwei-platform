// 入站分流决策。把决策点的分叉逻辑抽成纯函数：
//
//   flag off                  → local（完全旁路，零回归：不算 lane、不交接）
//   已交接的信封               → local（信封 lane 是权威，绝不再判一次）
//   flag on + 本进程非 prod    → local（泳道进程收到的都是判过的消息）
//   flag on + lane==本进程lane → local（prod 消息本地处理，绝不交接给自己）
//   flag on + lane!=本进程lane → dispatch（交接给目标泳道，本地不再处理）
//
// resolveLane 由调用方注入（生产=getLaneBindingResolver().resolveLane），决策只看渠道无关
// 的 channel + common conversation + 全局 bot 标识（平台无关红线）。

export interface InboundDispatchInput {
    // 动态 flag「是否启用处理层分流」（§3 / Task 10）。默认 off = 现状行为。
    flagEnabled: boolean;
    // 这条消息是不是从别的进程交接过来的（走 lane-inbound 端点进来的都是）。
    handedOff: boolean;
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
    // local = 本进程继续走入站后半段；dispatch = 交接给目标泳道。
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

    // 交接过来的信封：泳道在投递方那边已经判完，信封里的 lane 就是权威值。这里再判
    // 一次有两个坏处：绑定若在交接后变更，同一条消息会被二次转投；而 sidecar 在目标
    // 泳道不存在时把请求打回的正是 prod 自己，二次判定会得到同一个目标泳道、再交接一次
    // ——无限自投。这条分支是那个循环的唯一阻断点。
    if (input.handedOff) {
        return { action: 'local', lane: input.currentLane };
    }

    // 泳道进程收到的入站消息只会来自交接，上面那条分支已经处置完。走到这里说明本进程
    // 是泳道但消息不带交接标记，同样不查绑定：泳道不做分流决策。
    if (input.currentLane !== 'prod') {
        return { action: 'local', lane: input.currentLane };
    }

    const lane = await input.resolveLane(
        input.channel,
        input.botGlobalId,
        input.commonConversationId,
    );

    // lane == 本进程 lane（含 prod 占绝大多数）：本地处理，绝不交接给自己 —— 那会让
    // 同一条消息在本进程处理两遍。
    if (lane === input.currentLane) {
        return { action: 'local', lane };
    }

    // lane != 本进程 lane：交接给目标泳道，本进程到此为止。
    return { action: 'dispatch', lane };
}
