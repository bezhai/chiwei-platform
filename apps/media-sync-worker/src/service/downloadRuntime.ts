import { setTimeout as sleep } from 'timers/promises';

export interface DownloadDelayConfig {
    afterIllustInfoMs: number;
    beforePageDownloadMs: number;
    afterTaskMs: number;
    afterAuthorMs: number;
    limiterCooldownMs: number;
}

const DEFAULT_DELAYS: DownloadDelayConfig = {
    afterIllustInfoMs: 1500,
    beforePageDownloadMs: 1000,
    afterTaskMs: 2500,
    afterAuthorMs: 1500,
    limiterCooldownMs: 2 * 60 * 1000,
};

export function loadDownloadDelayConfig(
    env: Record<string, string | undefined> = process.env
): DownloadDelayConfig {
    return {
        afterIllustInfoMs: readNonNegativeMs(
            env.DOWNLOAD_AFTER_ILLUST_INFO_DELAY_MS,
            DEFAULT_DELAYS.afterIllustInfoMs
        ),
        beforePageDownloadMs: readNonNegativeMs(
            env.DOWNLOAD_BEFORE_PAGE_DOWNLOAD_DELAY_MS,
            DEFAULT_DELAYS.beforePageDownloadMs
        ),
        afterTaskMs: readNonNegativeMs(
            env.DOWNLOAD_AFTER_TASK_DELAY_MS,
            DEFAULT_DELAYS.afterTaskMs
        ),
        afterAuthorMs: readNonNegativeMs(
            env.DOWNLOAD_AFTER_AUTHOR_DELAY_MS,
            DEFAULT_DELAYS.afterAuthorMs
        ),
        limiterCooldownMs: readNonNegativeMs(
            env.DOWNLOAD_LIMITER_COOLDOWN_MS,
            DEFAULT_DELAYS.limiterCooldownMs
        ),
    };
}

export function nowMs(): number {
    return performance.now();
}

export function elapsedMs(startMs: number, getNow: () => number = nowMs): number {
    return Math.max(0, Math.round(getNow() - startMs));
}

export async function waitMs(ms: number): Promise<void> {
    if (ms <= 0) {
        return;
    }
    await sleep(ms);
}

function readNonNegativeMs(value: string | undefined, fallback: number): number {
    if (value === undefined || value === '') {
        return fallback;
    }
    const parsed = Number.parseInt(value, 10);
    if (!Number.isFinite(parsed) || parsed < 0) {
        return fallback;
    }
    return parsed;
}
