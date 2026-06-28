import { describe, it, expect } from 'bun:test';
import { loadConfig } from './config';

const FULL_ENV = {
    QQ_BOT_NAME: 'chiwei',
    INNER_HTTP_SECRET: 'inner-xyz',
    POSTGRES_HOST: 'pg.internal',
    POSTGRES_USER: 'pguser',
    POSTGRES_PASSWORD: 'pgpass',
    POSTGRES_DB: 'chiwei',
};

describe('loadConfig', () => {
    it('loads required values and applies sensible defaults', () => {
        const cfg = loadConfig({ ...FULL_ENV });
        expect(cfg.botName).toBe('chiwei');
        expect(cfg.innerSecret).toBe('inner-xyz');
        expect(cfg.postgres).toEqual({
            host: 'pg.internal',
            port: 5432,
            user: 'pguser',
            password: 'pgpass',
            db: 'chiwei',
        });
        // defaults
        expect(cfg.port).toBe(3000);
        expect(cfg.channelServerService).toBe('channel-server');
        expect(cfg.channelServerInboundPath).toBe('/api/internal/qq/inbound');
        expect(cfg.windowMs).toBe(60 * 60 * 1000);
        expect(cfg.maxReplies).toBe(4);
    });

    it('reads overridable port / redis / postgres port from env', () => {
        const cfg = loadConfig({
            ...FULL_ENV,
            PORT: '4000',
            REDIS_HOST: 'redis.internal',
            REDIS_PORT: '6380',
            REDIS_PASSWORD: 'pw',
            POSTGRES_PORT: '5433',
        });
        expect(cfg.port).toBe(4000);
        expect(cfg.redis).toEqual({ host: 'redis.internal', port: 6380, password: 'pw' });
        expect(cfg.postgres.port).toBe(5433);
    });

    it('does not expose QQ credentials on the config (no env fallback)', () => {
        const cfg = loadConfig({ ...FULL_ENV, QQ_APP_ID: 'x', QQ_APP_SECRET: 'y' });
        const bag = cfg as unknown as Record<string, unknown>;
        expect(bag.appId).toBeUndefined();
        expect(bag.appSecret).toBeUndefined();
    });

    it.each(['QQ_BOT_NAME', 'INNER_HTTP_SECRET', 'POSTGRES_HOST', 'POSTGRES_USER', 'POSTGRES_PASSWORD', 'POSTGRES_DB'])(
        'throws fast when required env %s is missing',
        (key) => {
            const env: Record<string, string> = { ...FULL_ENV };
            delete env[key];
            expect(() => loadConfig(env)).toThrow(new RegExp(key));
        },
    );
});
