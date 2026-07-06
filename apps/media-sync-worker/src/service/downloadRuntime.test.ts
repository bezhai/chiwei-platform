import { describe, expect, it } from 'bun:test';
import { loadConsumerGuardConfig, loadDownloadDelayConfig } from './downloadRuntime';

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

describe('loadConsumerGuardConfig', () => {
    it('defaults the Running reclaim threshold to 90 minutes', () => {
        expect(loadConsumerGuardConfig({}).runningTaskReclaimMs).toBe(90 * 60 * 1000);
    });

    it('accepts a positive RUNNING_TASK_RECLAIM_MS override', () => {
        const config = loadConsumerGuardConfig({ RUNNING_TASK_RECLAIM_MS: '7200000' });

        expect(config.runningTaskReclaimMs).toBe(7200000);
    });

    it('falls back to the default when RUNNING_TASK_RECLAIM_MS is non-positive or not a number', () => {
        expect(loadConsumerGuardConfig({ RUNNING_TASK_RECLAIM_MS: '0' }).runningTaskReclaimMs).toBe(
            90 * 60 * 1000
        );
        expect(loadConsumerGuardConfig({ RUNNING_TASK_RECLAIM_MS: 'abc' }).runningTaskReclaimMs).toBe(
            90 * 60 * 1000
        );
    });

    it('defaults the cycle watchdog timeout to 60 minutes', () => {
        expect(loadConsumerGuardConfig({}).cycleTimeoutMs).toBe(60 * 60 * 1000);
    });

    it('accepts a positive CONSUMER_CYCLE_TIMEOUT_MS override', () => {
        const config = loadConsumerGuardConfig({
            CONSUMER_CYCLE_TIMEOUT_MS: '1800000',
        });

        expect(config.cycleTimeoutMs).toBe(1800000);
    });

    it('falls back to the default when CONSUMER_CYCLE_TIMEOUT_MS is non-positive or not a number', () => {
        expect(loadConsumerGuardConfig({ CONSUMER_CYCLE_TIMEOUT_MS: '0' }).cycleTimeoutMs).toBe(
            60 * 60 * 1000
        );
        expect(loadConsumerGuardConfig({ CONSUMER_CYCLE_TIMEOUT_MS: '-5' }).cycleTimeoutMs).toBe(
            60 * 60 * 1000
        );
    });

    it('reverts both thresholds to defaults when reclaim is not strictly greater than cycle timeout', () => {
        // reclaim (1.5h) < cycle timeout (2h): watchdog-abandoned round could still be
        // running when its task gets re-claimed, so the pair is rejected as a whole
        expect(
            loadConsumerGuardConfig({
                CONSUMER_CYCLE_TIMEOUT_MS: '7200000',
                RUNNING_TASK_RECLAIM_MS: '5400000',
            })
        ).toEqual({
            cycleTimeoutMs: 60 * 60 * 1000,
            runningTaskReclaimMs: 90 * 60 * 1000,
        });

        // equality also violates the "strictly greater" invariant
        expect(
            loadConsumerGuardConfig({
                CONSUMER_CYCLE_TIMEOUT_MS: '3600000',
                RUNNING_TASK_RECLAIM_MS: '3600000',
            })
        ).toEqual({
            cycleTimeoutMs: 60 * 60 * 1000,
            runningTaskReclaimMs: 90 * 60 * 1000,
        });
    });

    it('keeps a valid override pair intact', () => {
        expect(
            loadConsumerGuardConfig({
                CONSUMER_CYCLE_TIMEOUT_MS: '1800000',
                RUNNING_TASK_RECLAIM_MS: '2700000',
            })
        ).toEqual({
            cycleTimeoutMs: 1800000,
            runningTaskReclaimMs: 2700000,
        });
    });
});
