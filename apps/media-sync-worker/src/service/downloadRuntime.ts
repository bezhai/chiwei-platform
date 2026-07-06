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

export interface ConsumerGuardConfig {
    cycleTimeoutMs: number;
    runningTaskReclaimMs: number;
}

// 60 分钟：最坏合法单轮 ≈ 20 页/并发 2 的大任务，每页代理请求最坏顶满 180s（10 批 ×
// 180s = 30min）+ 前置 info/pages 请求各最坏 180s + 限流冷却 120s + 翻译与 Mongo 杂项
// ≈ 40min；留余量后凡超过 60min 必是挂死而非慢。
const DEFAULT_CONSUMER_CYCLE_TIMEOUT_MS = 60 * 60 * 1000;

// 90 分钟：必须严格大于 consumer 单轮 watchdog 上限（60 分钟），保证被 watchdog 放弃
// 的轮次要么已被自身超时终结、要么其收尾早于回收发生，缩小"旧轮次 vs 新领取"并发窗口。
const DEFAULT_RUNNING_TASK_RECLAIM_MS = 90 * 60 * 1000;

export function loadConsumerGuardConfig(
    env: Record<string, string | undefined> = process.env
): ConsumerGuardConfig {
    const cycleTimeoutMs = readPositiveMs(
        env.CONSUMER_CYCLE_TIMEOUT_MS,
        DEFAULT_CONSUMER_CYCLE_TIMEOUT_MS
    );
    const runningTaskReclaimMs = readPositiveMs(
        env.RUNNING_TASK_RECLAIM_MS,
        DEFAULT_RUNNING_TASK_RECLAIM_MS
    );

    // 阈值耦合校验：回收阈值不大于 watchdog 上限时，被放弃的旧轮次可能还在跑就被
    // 重新领取。两个值是一对约束，任何一边越界都整对回退默认。
    if (runningTaskReclaimMs <= cycleTimeoutMs) {
        console.warn(
            `Invalid consumer guard config: RUNNING_TASK_RECLAIM_MS (${runningTaskReclaimMs}) must be ` +
                `strictly greater than CONSUMER_CYCLE_TIMEOUT_MS (${cycleTimeoutMs}); ` +
                `reverting both to defaults (${DEFAULT_RUNNING_TASK_RECLAIM_MS}/${DEFAULT_CONSUMER_CYCLE_TIMEOUT_MS}).`
        );
        return {
            cycleTimeoutMs: DEFAULT_CONSUMER_CYCLE_TIMEOUT_MS,
            runningTaskReclaimMs: DEFAULT_RUNNING_TASK_RECLAIM_MS,
        };
    }

    return { cycleTimeoutMs, runningTaskReclaimMs };
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

// 与 readNonNegativeMs 不同：阈值类配置取 0 没有合法语义（0 回收阈值会让所有 Running
// 任务立即可被重复领取），所以 0 也回退默认值。
function readPositiveMs(value: string | undefined, fallback: number): number {
    if (value === undefined || value === '') {
        return fallback;
    }
    const parsed = Number.parseInt(value, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
        return fallback;
    }
    return parsed;
}
