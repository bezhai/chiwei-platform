import { describe, expect, it } from 'bun:test';
import {
    loadRedisCommandTimeoutMs,
    withRedisCommandTimeout,
} from './config';

describe('loadRedisCommandTimeoutMs', () => {
    it('defaults media-sync Redis commands to a 30 second hard timeout', () => {
        expect(loadRedisCommandTimeoutMs({})).toBe(30_000);
    });

    it('accepts a positive override and rejects non-positive or invalid values', () => {
        expect(loadRedisCommandTimeoutMs({ REDIS_COMMAND_TIMEOUT_MS: '45000' })).toBe(45_000);
        expect(loadRedisCommandTimeoutMs({ REDIS_COMMAND_TIMEOUT_MS: '0' })).toBe(30_000);
        expect(loadRedisCommandTimeoutMs({ REDIS_COMMAND_TIMEOUT_MS: '-1' })).toBe(30_000);
        expect(loadRedisCommandTimeoutMs({ REDIS_COMMAND_TIMEOUT_MS: 'slow' })).toBe(30_000);
        expect(loadRedisCommandTimeoutMs({ REDIS_COMMAND_TIMEOUT_MS: '30000ms' })).toBe(30_000);
        expect(loadRedisCommandTimeoutMs({ REDIS_COMMAND_TIMEOUT_MS: '1.5' })).toBe(30_000);
        expect(loadRedisCommandTimeoutMs({ REDIS_COMMAND_TIMEOUT_MS: '2147483648' })).toBe(30_000);
    });
});

describe('withRedisCommandTimeout', () => {
    it('wires the parsed timeout into the shared client config without dropping base fields', () => {
        expect(
            withRedisCommandTimeout(
                {
                    host: 'redis.internal',
                    port: 6380,
                    password: 'secret',
                },
                { REDIS_COMMAND_TIMEOUT_MS: '45000' }
            )
        ).toEqual({
            host: 'redis.internal',
            port: 6380,
            password: 'secret',
            commandTimeout: 45_000,
        });
    });
});
