import { describe, expect, it } from 'bun:test';
import { validateTaggerFeatureFlags } from './runtime';

describe('validateTaggerFeatureFlags', () => {
    it('requires MinIO sync when automatic trigger is enabled', () => {
        expect(() => validateTaggerFeatureFlags(
            { triggerEnabled: true },
            { MINIO_SYNC_ENABLED: 'false' },
        )).toThrow('MINIO_SYNC_ENABLED');
    });

    it('accepts trigger with MinIO sync on', () => {
        expect(() => validateTaggerFeatureFlags(
            { triggerEnabled: true },
            { MINIO_SYNC_ENABLED: 'true' },
        )).not.toThrow();
    });

    it('accepts an intentionally disabled tagger runtime', () => {
        expect(() => validateTaggerFeatureFlags(
            { triggerEnabled: false },
            {},
        )).not.toThrow();
    });
});
