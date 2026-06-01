// 平台无关的泳道决策能力（lane-routing-redesign §3）。本期只做 bot 维度：
// 在 lark common projector 收敛出 common id 之后，基于平台无关的「全局 Bot
// 概念」算出这条消息该走哪个 lane。
//
// 决策优先级本期就两档（§3.2）：
//   bot 维度命中 lane_routing  >  prod（默认）
//
// 平台无关红线（§3.2）：resolveLane 的入参只认 channel + 全局 bot 标识，
// 绝不出现任何飞书原始消息体字段名。平台插件负责把渠道内标识收敛成 common
// 口径，决策层只认 common 口径。
//
// 与 ORM 解耦：本模块只依赖结构型接口 LaneRoutingStore，不 import 任何
// TypeORM 实体或数据源，单测可纯跑。生产运行时由 infrastructure 层提供一个
// TypeORM 实现（读 lane_routing 表 route_type=Bot）注入进来。
//
// 缓存：沿用 lane_routing 决策已验证的 30s 内存缓存语义——决策走
// 本地缓存，不为每条消息打 DB；缓存 key 含 channel + 全局 bot 标识，跨 channel
// 同名 bot 不串。clearCache 主动失效能力不能丢：L6 admin 改绑定 / `/ops bind`
// 后要本进程直接调它，把「改绑定后最多 30s 才生效」的窗口压到接近零（§3.3）。

// 未命中任何绑定时的默认 lane。prod 是绝大多数流量的归属，也是「没绑定 = 走线上」
// 的语义落点。
const DEFAULT_LANE = 'prod';

// 决策缓存 TTL，沿用 lane_routing 决策的 30s（§3.3 已验证）。
const CACHE_TTL_MS = 30_000;

// LaneRouter 对底层存储的全部需求。结构型接口，不绑 ORM。
// 本期只有 bot 维度，所以只暴露按全局 bot 标识查 lane 这一个能力。
export interface LaneRoutingStore {
    // 按全局 bot 标识查它当前绑定的 lane（对应 lane_routing 表
    // route_type=Bot AND route_key=botGlobalId AND is_active=true）。
    // 没有绑定返回 null。
    findBotLane(botGlobalId: string): Promise<string | null>;
}

interface CacheEntry {
    lane: string;
    expiry: number;
}

export class LaneRouter {
    private cache = new Map<string, CacheEntry>();

    // now 可注入：生产用真实时钟；单测注入可控时钟以确定性地验证 TTL 行为。
    constructor(
        private readonly store: LaneRoutingStore,
        private readonly now: () => number = Date.now,
    ) {}

    // 平台无关的泳道决策：只认 channel + 全局 bot 标识。
    // 命中绑定返回 lane_name，未命中返回 prod 默认。
    async resolveLane(channel: string, botGlobalId: string): Promise<string> {
        const cacheKey = `${channel}:${botGlobalId}`;
        const now = this.now();

        const cached = this.cache.get(cacheKey);
        if (cached && cached.expiry > now) {
            return cached.lane;
        }

        const bound = await this.store.findBotLane(botGlobalId);
        const lane = bound ?? DEFAULT_LANE;
        this.cache.set(cacheKey, { lane, expiry: now + CACHE_TTL_MS });
        return lane;
    }

    // 主动失效全部决策缓存。绑定变更（admin 改绑定 / `/ops bind`）后由同进程
    // 直接调用，让新绑定不必等 30s TTL 即刻生效（§3.3）。
    clearCache(): void {
        this.cache.clear();
    }
}
