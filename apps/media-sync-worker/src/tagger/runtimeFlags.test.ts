import { describe, expect, it } from 'bun:test';
import { validateTaggerFeatureFlags } from './runtime';

describe('validateTaggerFeatureFlags', () => {
    it('requires MinIO and local mirror when automatic trigger is enabled', () => {
        expect(() => validateTaggerFeatureFlags(
            { triggerEnabled: true, projectionEnabled: false },
            { MINIO_SYNC_ENABLED: 'false', PIXIV_IMAGE_MIRROR_MONGO_ENABLED: 'true' },
        )).toThrow('MINIO_SYNC_ENABLED');

        expect(() => validateTaggerFeatureFlags(
            { triggerEnabled: true, projectionEnabled: false },
            { MINIO_SYNC_ENABLED: 'true', PIXIV_IMAGE_MIRROR_MONGO_ENABLED: 'false' },
        )).toThrow('PIXIV_IMAGE_MIRROR_MONGO_ENABLED');
    });

    it('requires local mirror when result projection is enabled', () => {
        expect(() => validateTaggerFeatureFlags(
            { triggerEnabled: false, projectionEnabled: true },
            { PIXIV_IMAGE_MIRROR_MONGO_ENABLED: 'false' },
        )).toThrow('PIXIV_IMAGE_MIRROR_MONGO_ENABLED');
    });

    it('accepts an intentionally disabled tagger runtime', () => {
        expect(() => validateTaggerFeatureFlags(
            { triggerEnabled: false, projectionEnabled: false },
            {},
        )).not.toThrow();
    });
});
