// 渠道无关的泳道归属决策：一条入站消息该由哪个泳道处理。
//
// 决策优先级：
//   会话维度命中绑定 > bot 维度命中绑定 > prod（默认）
//
// 为什么它渠道无关：resolveLane 的 channel 参数是**数据**不是**代码知识** ——
// 它只当字符串 key 参与缓存键，本模块不 import 任何渠道实现，也不认识任何具体
// 取值。各渠道插件负责把渠道内标识收敛成 common 口径（common conversation id /
// 全局 bot 标识），决策层只认 common 口径。
//
// 命名：本包里另有一个 LaneRouter（'@inner/shared' 根导出），那个回答的是
// 「某个服务的某个泳道实例在哪个地址」（服务发现）。这里回答的是「这条消息属于
// 哪个泳道」（绑定解析），是完全不同的东西 —— 所以刻意不叫 LaneRouter，沿用项目
// 既有的 "lane binding" 词汇（lane-bindings API / `/ops bind`）。
//
// 与 ORM 解耦：本模块只依赖结构型接口 LaneBindingStore，不 import 任何 TypeORM
// 实体或数据源，单测可纯跑。运行时由 ./store.ts 的 TypeORM 实现注入。

// 未命中任何绑定时的默认 lane。prod 是绝大多数流量的归属，也是「没绑定 = 走线上」
// 的语义落点。
const DEFAULT_LANE = 'prod';

// 决策缓存 TTL。决策走本地缓存，不为每条消息打 DB。
const CACHE_TTL_MS = 30_000;

// LaneBindingResolver 对底层存储的全部需求。结构型接口，不绑 ORM。
export interface LaneBindingStore {
    // 按渠道无关的 common_conversation_id 查 lane（对应 lane_routing 表
    // route_type=chat AND route_key=commonConversationId AND is_active=true）。
    // 没有绑定返回 null。
    findChatLane(commonConversationId: string): Promise<string | null>;
    // 按全局 bot 标识查它当前绑定的 lane（对应 lane_routing 表
    // route_type=bot AND route_key=botGlobalId AND is_active=true）。
    // 没有绑定返回 null。
    findBotLane(botGlobalId: string): Promise<string | null>;
}

interface CacheEntry {
    lane: string;
    expiry: number;
}

export class LaneBindingResolver {
    private cache = new Map<string, CacheEntry>();

    // now 可注入：生产用真实时钟；单测注入可控时钟以确定性地验证 TTL 行为。
    constructor(
        private readonly store: LaneBindingStore,
        private readonly now: () => number = Date.now,
    ) {}

    // 渠道无关的泳道决策：只认 channel key + common conversation + 全局 bot 标识。
    // 命中绑定返回 lane_name，未命中返回 prod 默认。
    async resolveLane(
        channel: string,
        botGlobalId: string,
        commonConversationId: string | undefined,
    ): Promise<string> {
        // 缓存 key 含 channel：跨渠道同名 bot 不串。
        const cacheKey = `${channel}:${commonConversationId ?? '-'}:${botGlobalId}`;
        const now = this.now();

        const cached = this.cache.get(cacheKey);
        if (cached && cached.expiry > now) {
            return cached.lane;
        }

        const bound =
            commonConversationId !== undefined
                ? await this.store.findChatLane(commonConversationId)
                : null;
        if (bound !== null) {
            this.cache.set(cacheKey, { lane: bound, expiry: now + CACHE_TTL_MS });
            return bound;
        }

        const botBound = await this.store.findBotLane(botGlobalId);
        const lane = botBound ?? DEFAULT_LANE;
        this.cache.set(cacheKey, { lane, expiry: now + CACHE_TTL_MS });
        return lane;
    }

    // 主动失效全部决策缓存。绑定变更（lane-bindings admin API / `/ops bind`）后由
    // 同进程直接调用，把「改绑定后最多 30s 才生效」的窗口压到接近零。
    clearCache(): void {
        this.cache.clear();
    }
}
