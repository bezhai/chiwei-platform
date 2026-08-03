import { describe, expect, it } from 'bun:test';

import { loadConfig } from './config';

// 一份「全都配齐」的 env，各用例从它出发只删/改自己关心的那一项。
const COMPLETE_ENV = {
    POSTGRES_HOST: 'pg.internal',
    POSTGRES_USER: 'chiwei',
    POSTGRES_PASSWORD: 'secret',
    POSTGRES_DB: 'chiwei',
    REDIS_HOST: 'redis.internal',
    RABBITMQ_URL: 'amqp://mq.internal',
};

describe('loadConfig', () => {
    it('defaults the HTTP port to 3000', () => {
        expect(loadConfig(COMPLETE_ENV).port).toBe(3000);
    });

    it('honours PORT', () => {
        expect(loadConfig({ ...COMPLETE_ENV, PORT: '8080' }).port).toBe(8080);
    });

    it('rejects a PORT that is not a number', () => {
        expect(() => loadConfig({ ...COMPLETE_ENV, PORT: 'http' })).toThrow(/PORT/);
    });

    // 缺 env 的症状本来是「服务起来了、第一条消息才炸」，而且报错来自 pg / amqplib
    // 底层，看不出是哪个 key 没配。启动期一次性列全，部署时一眼能补齐。
    it('names every missing env at once instead of failing one at a time', () => {
        const partial = { POSTGRES_HOST: 'pg.internal', POSTGRES_USER: 'chiwei' };
        let message = '';
        try {
            loadConfig(partial);
        } catch (error) {
            message = (error as Error).message;
        }
        expect(message).toContain('POSTGRES_PASSWORD');
        expect(message).toContain('POSTGRES_DB');
        expect(message).toContain('REDIS_HOST');
        expect(message).toContain('RABBITMQ_URL');
        expect(message).not.toContain('POSTGRES_HOST');
    });

    it.each(Object.keys(COMPLETE_ENV))('fails closed when %s is missing', (key) => {
        const env: Record<string, string | undefined> = { ...COMPLETE_ENV };
        delete env[key];
        expect(() => loadConfig(env)).toThrow(new RegExp(key));
    });

    it('treats an empty string as missing', () => {
        expect(() => loadConfig({ ...COMPLETE_ENV, RABBITMQ_URL: '' })).toThrow(/RABBITMQ_URL/);
    });
});
