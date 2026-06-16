import { describe, expect, it } from 'bun:test';
import { loadPixivImageMirrorMongoConfig } from './imageMirrorConfig';

describe('loadPixivImageMirrorMongoConfig', () => {
    it('returns null when mirror is not explicitly enabled', () => {
        const config = loadPixivImageMirrorMongoConfig({
            PIXIV_IMAGE_MIRROR_MONGO_HOST: 'local-mongo',
        });

        expect(config).toBeNull();
    });

    it('does not fall back to legacy source MONGO_* envs', () => {
        expect(() =>
            loadPixivImageMirrorMongoConfig({
                PIXIV_IMAGE_MIRROR_MONGO_ENABLED: 'true',
                MONGO_HOST: 'legacy-source-mongo',
                MONGO_INITDB_ROOT_USERNAME: 'legacy-user',
                MONGO_INITDB_ROOT_PASSWORD: 'legacy-pass',
            })
        ).toThrow('PIXIV_IMAGE_MIRROR_MONGO_HOST is required');
    });

    it('parses explicit local mirror Mongo envs', () => {
        const config = loadPixivImageMirrorMongoConfig({
            PIXIV_IMAGE_MIRROR_MONGO_ENABLED: '1',
            PIXIV_IMAGE_MIRROR_MONGO_HOST: 'local-mongo',
            PIXIV_IMAGE_MIRROR_MONGO_PORT: '27018',
            PIXIV_IMAGE_MIRROR_MONGO_DATABASE: 'chiwei_pixiv',
            PIXIV_IMAGE_MIRROR_MONGO_USERNAME: 'root',
            PIXIV_IMAGE_MIRROR_MONGO_PASSWORD: 'secret',
            PIXIV_IMAGE_MIRROR_MONGO_AUTH_SOURCE: 'admin',
            PIXIV_IMAGE_MIRROR_MONGO_CONNECT_TIMEOUT_MS: '3000',
        });

        expect(config).toEqual({
            host: 'local-mongo',
            port: 27018,
            database: 'chiwei_pixiv',
            username: 'root',
            password: 'secret',
            authSource: 'admin',
            connectTimeoutMS: 3000,
        });
    });

    it('defaults the target database and omits port when host already contains one', () => {
        const config = loadPixivImageMirrorMongoConfig({
            PIXIV_IMAGE_MIRROR_MONGO_ENABLED: 'true',
            PIXIV_IMAGE_MIRROR_MONGO_HOST: 'local-mongo:27017',
            PIXIV_IMAGE_MIRROR_MONGO_PORT: '27018',
        });

        expect(config?.host).toBe('local-mongo:27017');
        expect(config?.port).toBeUndefined();
        expect(config?.database).toBe('chiwei_pixiv');
    });
});
