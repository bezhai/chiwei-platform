/**
 * DynamicConfig — 运行时动态配置 SDK (TypeScript)
 *
 * 用法:
 *   import { DynamicConfig } from 'ts-shared/dynamic-config'
 *
 *   const config = new DynamicConfig({ laneProvider: () => context.get('lane') })
 *   const model = await config.get("default_model", "gemini")
 *   const threshold = await config.getFloat("proactive_threshold", 0.7)
 */

import { context } from '../middleware/context';

const CACHE_TTL = 10_000; // 10 seconds in ms

interface ConfigEntry {
    value: string;
    lane: string;
}

interface ResolvedResponse {
    data?: {
        configs: Record<string, ConfigEntry>;
        resolved_at: string;
    };
    configs?: Record<string, ConfigEntry>;
}

interface CacheEntry {
    snapshot: Record<string, ConfigEntry>;
    expireAt: number;
}

export interface DynamicConfigOptions {
    paasEngineUrl?: string;
    laneProvider?: () => string | undefined;
}

export class DynamicConfig {
    private paasEngineUrl: string;
    private laneProvider: () => string | undefined;
    private cache: Map<string, CacheEntry> = new Map();

    constructor(options: DynamicConfigOptions = {}) {
        this.paasEngineUrl = (options.paasEngineUrl || 'http://paas-engine:8080').replace(/\/+$/, '');
        this.laneProvider = options.laneProvider || (() => context.get<string>('lane'));
    }

    private getLane(): string {
        const lane = this.laneProvider();
        return lane || 'prod';
    }

    private async fetchSnapshot(lane: string): Promise<Record<string, ConfigEntry>> {
        try {
            const url = `${this.paasEngineUrl}/internal/dynamic-config/resolved?lane=${encodeURIComponent(lane)}`;
            const resp = await fetch(url, { signal: AbortSignal.timeout(5000) });
            if (resp.ok) {
                const body: ResolvedResponse = await resp.json();
                const data = body.data ?? body;
                return data.configs ?? {};
            }
            console.warn(`[DynamicConfig] paas-engine responded ${resp.status}`);
        } catch (err) {
            console.warn('[DynamicConfig] failed to fetch config:', err);
        }
        return {};
    }

    private async getSnapshot(lane: string): Promise<Record<string, ConfigEntry>> {
        const now = Date.now();
        const cached = this.cache.get(lane);
        if (cached && now < cached.expireAt) {
            return cached.snapshot;
        }
        const snapshot = await this.fetchSnapshot(lane);
        this.cache.set(lane, { snapshot, expireAt: now + CACHE_TTL });
        return snapshot;
    }

    async get(key: string, defaultValue: string = ''): Promise<string> {
        const lane = this.getLane();
        const snapshot = await this.getSnapshot(lane);
        return snapshot[key]?.value ?? defaultValue;
    }

    async getInt(key: string, defaultValue: number = 0): Promise<number> {
        const raw = await this.get(key);
        if (raw === '') return defaultValue;
        const n = parseInt(raw, 10);
        return isNaN(n) ? defaultValue : n;
    }

    async getFloat(key: string, defaultValue: number = 0): Promise<number> {
        const raw = await this.get(key);
        if (raw === '') return defaultValue;
        const n = parseFloat(raw);
        return isNaN(n) ? defaultValue : n;
    }

    async getBool(key: string, defaultValue: boolean = false): Promise<boolean> {
        const raw = await this.get(key);
        if (raw === '') return defaultValue;
        return ['true', '1', 'yes'].includes(raw.toLowerCase());
    }
}
