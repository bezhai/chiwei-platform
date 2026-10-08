import { expect, it } from 'bun:test';
import { withRedisCommandTimeout } from './config';
import Redis from 'ioredis';

it('adds a command deadline only to worker Redis configuration', () => {
    const base = { host: 'redis', port: 6379, password: 'test' };
    expect(withRedisCommandTimeout(base, {})).toEqual({ ...base, commandTimeout: 10_000 });
    expect(base).not.toHaveProperty('commandTimeout');
    expect(withRedisCommandTimeout(base, { REDIS_COMMAND_TIMEOUT_MS: '25' }).commandTimeout).toBe(25);
    for (const value of ['0', '-1', '10ms', 'NaN']) {
        expect(() => withRedisCommandTimeout(base, { REDIS_COMMAND_TIMEOUT_MS: value })).toThrow();
    }
});

it('rejects a command when the TCP peer accepts connections but never replies', async () => {
    const server = Bun.listen({
        hostname: '127.0.0.1', port: 0,
        socket: { data() {}, error() {} },
    });
    const client = new Redis({
        ...withRedisCommandTimeout({ host: '127.0.0.1', port: server.port }, { REDIS_COMMAND_TIMEOUT_MS: '25' }),
        enableReadyCheck: false, lazyConnect: true,
    });
    client.on('error', () => {});
    try {
        await client.connect();
        await expect(client.ping()).rejects.toThrow('Command timed out');
    } finally {
        client.disconnect();
        server.stop(true);
    }
});
