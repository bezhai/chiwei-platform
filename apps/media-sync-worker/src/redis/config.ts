import type { RedisConfig } from '@inner/shared/cache';
import { positiveTimeout } from '../service/discoveryStep';

export function withRedisCommandTimeout(
    config: RedisConfig,
    env: Record<string, string | undefined> = process.env,
): RedisConfig {
    return {
        ...config,
        commandTimeout: positiveTimeout(env.REDIS_COMMAND_TIMEOUT_MS, 10_000, 'REDIS_COMMAND_TIMEOUT_MS'),
    };
}
