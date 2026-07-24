import type { RedisConfig } from '@inner/shared/cache';

const DEFAULT_REDIS_COMMAND_TIMEOUT_MS = 30_000;
const MAX_TIMER_DELAY_MS = 2_147_483_647;

export function loadRedisCommandTimeoutMs(
    env: Record<string, string | undefined> = process.env
): number {
    const raw = env.REDIS_COMMAND_TIMEOUT_MS;
    if (raw === undefined || raw === '') {
        return DEFAULT_REDIS_COMMAND_TIMEOUT_MS;
    }
    const normalized = raw.trim();
    if (!/^\d+$/.test(normalized)) {
        return DEFAULT_REDIS_COMMAND_TIMEOUT_MS;
    }
    const parsed = Number(normalized);
    return Number.isSafeInteger(parsed) && parsed > 0 && parsed <= MAX_TIMER_DELAY_MS
        ? parsed
        : DEFAULT_REDIS_COMMAND_TIMEOUT_MS;
}

export function withRedisCommandTimeout(
    baseConfig: RedisConfig,
    env: Record<string, string | undefined> = process.env
): RedisConfig {
    return {
        ...baseConfig,
        commandTimeout: loadRedisCommandTimeoutMs(env),
    };
}
