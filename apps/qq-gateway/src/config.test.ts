import { describe, it, expect } from 'bun:test';
import { loadConfig } from './config';

const FULL_ENV = {
    QQ_APP_ID: 'app-123',
    QQ_APP_SECRET: 'secret-123',
    QQ_BOT_NAME: 'chiwei',
    INNER_HTTP_SECRET: 'inner-xyz',
};

describe('loadConfig', () => {
    it('loads required values and applies sensible defaults', () => {
        const cfg = loadConfig({ ...FULL_ENV });
        expect(cfg.appId).toBe('app-123');
        expect(cfg.appSecret).toBe('secret-123');
        expect(cfg.botName).toBe('chiwei');
        expect(cfg.innerSecret).toBe('inner-xyz');
        // defaults
        expect(cfg.port).toBe(3000);
        expect(cfg.channelServerService).toBe('channel-server');
        expect(cfg.channelServerInboundPath).toBe('/api/internal/qq/inbound');
        expect(cfg.windowMs).toBe(60 * 60 * 1000);
        expect(cfg.maxReplies).toBe(4);
    });

    it('reads overridable port / redis from env', () => {
        const cfg = loadConfig({
            ...FULL_ENV,
            PORT: '4000',
            REDIS_HOST: 'redis.internal',
            REDIS_PORT: '6380',
            REDIS_PASSWORD: 'pw',
        });
        expect(cfg.port).toBe(4000);
        expect(cfg.redis).toEqual({ host: 'redis.internal', port: 6380, password: 'pw' });
    });

    it.each(['QQ_APP_ID', 'QQ_APP_SECRET', 'QQ_BOT_NAME', 'INNER_HTTP_SECRET'])(
        'throws fast when required env %s is missing',
        (key) => {
            const env: Record<string, string> = { ...FULL_ENV };
            delete env[key];
            expect(() => loadConfig(env)).toThrow(new RegExp(key));
        },
    );
});
