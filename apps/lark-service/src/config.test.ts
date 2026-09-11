import { describe, expect, it } from 'bun:test';

import { loadConfig, loadOutboundConfig } from './config';

// 一份「全都配齐」的 env，各用例从它出发只删/改自己关心的那一项。
const COMPLETE_ENV = {
    POSTGRES_HOST: 'pg.internal',
    POSTGRES_USER: 'chiwei',
    POSTGRES_PASSWORD: 'secret',
    POSTGRES_DB: 'chiwei',
    REDIS_HOST: 'redis.internal',
    RABBITMQ_URL: 'amqp://mq.internal',
    MONGO_HOST: 'mongo.internal',
    // 飞书专属业务（Task D）要的下游凭据与地址。缺了都不报错、只是功能静默失效，
    // 所以一律进启动期的存在性检查。
    INNER_HTTP_SECRET: 'inner-secret',
    MINIO_ENDPOINT: 'minio.internal',
    MINIO_ACCESS_KEY: 'minio-key',
    MINIO_SECRET_KEY: 'minio-secret',
    MEME_HOST: 'http://meme.internal',
    MEME_PORT: '8080',
    AI_PROVIDER_ADMIN_KEY: 'provider-admin',
};

/** 上面那批里只有入口进程用得上的。出站进程一个都不该要。 */
const INGRESS_ONLY_KEYS = [
    'MINIO_ENDPOINT',
    'MINIO_ACCESS_KEY',
    'MINIO_SECRET_KEY',
    'MEME_HOST',
    'MEME_PORT',
    'AI_PROVIDER_ADMIN_KEY',
];

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
        expect(message).toContain('MONGO_HOST');
        expect(message).not.toContain('POSTGRES_HOST');
    });

    // 下面那条 it.each 逐个删 COMPLETE_ENV 的 key，飞书业务凭据也在其中。它们缺了都
    // 不会报错、只会静默降级（附件管线发出 `Bearer undefined`、meme 请求打到
    // `undefined:undefined/memes/list`、查余额拿 401），所以跟连接串同等对待。

    it.each(Object.keys(COMPLETE_ENV))('fails closed when %s is missing', (key) => {
        const env: Record<string, string | undefined> = { ...COMPLETE_ENV };
        delete env[key];
        expect(() => loadConfig(env)).toThrow(new RegExp(key));
    });

    it('treats an empty string as missing', () => {
        expect(() => loadConfig({ ...COMPLETE_ENV, RABBITMQ_URL: '' })).toThrow(/RABBITMQ_URL/);
    });
});

// 出站进程要的 env 比入口少一项：它不写 lark_event 审计（原始报文在入口那一侧第一次
// 进来时就记过了），所以不该因为 MONGO_HOST 没配就起不来。
describe('loadOutboundConfig', () => {
    const OUTBOUND = {
        POSTGRES_HOST: 'pg.internal',
        POSTGRES_USER: 'chiwei',
        POSTGRES_PASSWORD: 'secret',
        POSTGRES_DB: 'chiwei',
        REDIS_HOST: 'redis.internal',
        RABBITMQ_URL: 'amqp://mq.internal',
        // 出站也打 tool-service：她带的图在队列里是对象存储的永久句柄，发之前要向
        // tool-service 现签一个可下载地址（见 lark/outbound/fetch-picture.ts）。
        INNER_HTTP_SECRET: 'inner-secret',
    };

    it('does not require MONGO_HOST', () => {
        expect(() => loadOutboundConfig(OUTBOUND)).not.toThrow();
    });

    // 指令、附件管线、定时任务、卡片回调全挂在入站那一侧。出站只把赤尾的动作送到
    // 飞书，多要一个 key 只是多一个起不来的理由。
    it('does not require the ingress-only credentials', () => {
        expect(() => loadOutboundConfig(OUTBOUND)).not.toThrow();

        // 一个 env 都没配时报的那份清单里也不该有它们 —— 否则就是有人把入口专属的
        // 凭据加进了两个进程共用的那份清单。
        let message = '';
        try {
            loadOutboundConfig({});
        } catch (error) {
            message = (error as Error).message;
        }
        for (const key of INGRESS_ONLY_KEYS) {
            expect(message).not.toContain(key);
        }
    });

    // 缺了它，现签那一跳发出的是 `Bearer undefined`，tool-service 401 —— 而这一路的
    // 失败是降级：她的话照常发出去，只是图永远发不出来，全程没有任何东西变红。
    it('fails closed when INNER_HTTP_SECRET is missing', () => {
        const env: Record<string, string | undefined> = { ...OUTBOUND };
        delete env.INNER_HTTP_SECRET;
        expect(() => loadOutboundConfig(env)).toThrow(/INNER_HTTP_SECRET/);
    });

    it.each(Object.keys(OUTBOUND))('fails closed when %s is missing', (key) => {
        const env: Record<string, string | undefined> = { ...OUTBOUND };
        delete env[key];
        expect(() => loadOutboundConfig(env)).toThrow(new RegExp(key));
    });

    it('defaults the metrics port to 9091', () => {
        expect(loadOutboundConfig(OUTBOUND).metricsPort).toBe(9091);
    });

    it('honours METRICS_PORT', () => {
        expect(loadOutboundConfig({ ...OUTBOUND, METRICS_PORT: '9200' }).metricsPort).toBe(9200);
    });

    it('rejects a METRICS_PORT that is not a number', () => {
        expect(() => loadOutboundConfig({ ...OUTBOUND, METRICS_PORT: 'nine' })).toThrow(
            /METRICS_PORT/,
        );
    });
});
