import { Pool } from 'pg';

interface CacheEntry {
    lane: string | null;
    expiry: number;
}

/**
 * Lane 路由策略查询 + 内存缓存
 * 根据 route_type (bot/chat) + route_key 查询 lane_routing 表
 */
export class LaneResolver {
    private cache = new Map<string, CacheEntry>();
    private TTL = 30_000; // 30s

    constructor(private pool: Pool) {}

    async resolve(routeType: 'bot' | 'chat', routeKey: string): Promise<string | null> {
        const cacheKey = `${routeType}:${routeKey}`;
        const now = Date.now();

        const cached = this.cache.get(cacheKey);
        if (cached && cached.expiry > now) {
            return cached.lane;
        }

        const result = await this.pool.query<{ lane_name: string }>(
            'SELECT lane_name FROM lane_routing WHERE route_type = $1 AND route_key = $2 AND is_active = true',
            [routeType, routeKey],
        );

        const lane = result.rows.length > 0 ? result.rows[0].lane_name : null;
        this.cache.set(cacheKey, { lane, expiry: now + this.TTL });
        return lane;
    }

    /**
     * 清除缓存（用于测试或 bind/unbind 后立即生效）
     */
    clearCache(): void {
        this.cache.clear();
    }
}
