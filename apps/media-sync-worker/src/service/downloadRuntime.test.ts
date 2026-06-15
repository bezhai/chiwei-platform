import { describe, expect, it } from 'bun:test';
import { loadDownloadDelayConfig } from './downloadRuntime';

describe('loadDownloadDelayConfig', () => {
    it('defaults artificial waits to half of the old hard-coded values', () => {
        expect(loadDownloadDelayConfig({})).toEqual({
            afterIllustInfoMs: 1500,
            beforePageDownloadMs: 1000,
            afterTaskMs: 2500,
            afterAuthorMs: 1500,
            limiterCooldownMs: 120000,
        });
    });

    it('allows explicit zero/positive env overrides and ignores invalid values', () => {
        const config = loadDownloadDelayConfig({
            DOWNLOAD_AFTER_ILLUST_INFO_DELAY_MS: '0',
            DOWNLOAD_BEFORE_PAGE_DOWNLOAD_DELAY_MS: '250',
            DOWNLOAD_AFTER_TASK_DELAY_MS: '-1',
            DOWNLOAD_AFTER_AUTHOR_DELAY_MS: 'abc',
            DOWNLOAD_LIMITER_COOLDOWN_MS: '60000',
        });

        expect(config).toEqual({
            afterIllustInfoMs: 0,
            beforePageDownloadMs: 250,
            afterTaskMs: 2500,
            afterAuthorMs: 1500,
            limiterCooldownMs: 60000,
        });
    });
});
