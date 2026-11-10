import { describe, expect, it } from 'bun:test';
import { loadPixivMongoConfig } from './config';

describe('loadPixivMongoConfig', () => {
    it('is required: there is no enable switch, a missing host is a startup error', () => {
        expect(() => loadPixivMongoConfig({})).toThrow('PIXIV_MONGO_HOST is required');
    });

    it('parses every PIXIV_MONGO_* suffix', () => {
        expect(
            loadPixivMongoConfig({
                PIXIV_MONGO_HOST: 'mongo',
                PIXIV_MONGO_PORT: '27018',
                PIXIV_MONGO_DATABASE: 'chiwei_pixiv_test',
                PIXIV_MONGO_USERNAME: 'root',
                PIXIV_MONGO_PASSWORD: 'secret',
                PIXIV_MONGO_AUTH_SOURCE: 'admin2',
                PIXIV_MONGO_CONNECT_TIMEOUT_MS: '3000',
                PIXIV_MONGO_SOCKET_TIMEOUT_MS: '30000',
            })
        ).toEqual({
            host: 'mongo',
            port: 27018,
            database: 'chiwei_pixiv_test',
            username: 'root',
            password: 'secret',
            authSource: 'admin2',
            connectTimeoutMS: 3000,
            socketTimeoutMS: 30000,
        });
    });

    // socketTimeoutMS defaults to 60s: the driver default is 0 (wait forever), and a
    // half-open connection then hangs the consumer's claim loop silently (2026-06-27).
    // This connection now carries the download queue, so it inherits that guard.
    it('defaults to chiwei_pixiv, admin auth, and a 60s socket timeout', () => {
        expect(loadPixivMongoConfig({ PIXIV_MONGO_HOST: 'mongo' })).toEqual({
            host: 'mongo',
            port: undefined,
            database: 'chiwei_pixiv',
            username: undefined,
            password: undefined,
            authSource: 'admin',
            connectTimeoutMS: 2000,
            socketTimeoutMS: 60000,
        });
    });

    it('treats PIXIV_MONGO_SOCKET_TIMEOUT_MS=0 as an explicit off switch', () => {
        expect(
            loadPixivMongoConfig({ PIXIV_MONGO_HOST: 'mongo', PIXIV_MONGO_SOCKET_TIMEOUT_MS: '0' }).socketTimeoutMS
        ).toBe(0);
    });

    it('rejects malformed numbers instead of silently using a default', () => {
        expect(() =>
            loadPixivMongoConfig({ PIXIV_MONGO_HOST: 'mongo', PIXIV_MONGO_PORT: 'abc' })
        ).toThrow('PIXIV_MONGO_PORT');
        expect(() =>
            loadPixivMongoConfig({ PIXIV_MONGO_HOST: 'mongo', PIXIV_MONGO_SOCKET_TIMEOUT_MS: '-1' })
        ).toThrow('PIXIV_MONGO_SOCKET_TIMEOUT_MS');
    });

    it('omits the port when the host already contains one', () => {
        const config = loadPixivMongoConfig({ PIXIV_MONGO_HOST: 'mongo:27019', PIXIV_MONGO_PORT: '27018' });

        expect(config.host).toBe('mongo:27019');
        expect(config.port).toBeUndefined();
    });
});
