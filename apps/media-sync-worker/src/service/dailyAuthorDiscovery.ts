import type { DailyAuthor } from './dailyAuthorBatch';

export interface DailyAuthorDiscoveryDependencies {
    getLastDownloadTime: (authorId: string) => Promise<string | null>;
    discoverAuthor: (authorId: string, signal: AbortSignal) => Promise<void>;
    waitAfterAuthor: () => Promise<void>;
    getRandomDays: () => number;
    now: () => number;
}

export type DailyAuthorDiscoveryResult = 'completed' | 'skipped';

export async function runDailyAuthorDiscovery(
    author: DailyAuthor,
    dependencies: DailyAuthorDiscoveryDependencies,
    signal: AbortSignal
): Promise<DailyAuthorDiscoveryResult> {
    throwIfAborted(signal);
    const lastDownloadTime = await dependencies.getLastDownloadTime(author.userId);
    throwIfAborted(signal);

    if (
        lastDownloadTime !== null &&
        isInsideCooldown(
            lastDownloadTime,
            dependencies.getRandomDays(),
            dependencies.now()
        )
    ) {
        return 'skipped';
    }

    await dependencies.discoverAuthor(author.userId, signal);
    throwIfAborted(signal);

    await dependencies.waitAfterAuthor();
    throwIfAborted(signal);
    return 'completed';
}

export async function enqueueDownloadTasks(
    illustIds: readonly string[],
    insertTask: (illustId: string) => Promise<boolean>,
    signal: AbortSignal
): Promise<void> {
    for (const illustId of illustIds) {
        throwIfAborted(signal);
        await insertTask(illustId);
        throwIfAborted(signal);
    }
}

export function throwIfAborted(signal: AbortSignal): void {
    if (!signal.aborted) {
        return;
    }
    throw signal.reason ?? new Error('daily author discovery aborted');
}

function isInsideCooldown(
    lastDownloadTime: string,
    cooldownDays: number,
    nowMs: number
): boolean {
    const lastDownloadTimestamp = Number.parseInt(lastDownloadTime, 10);
    if (!Number.isFinite(lastDownloadTimestamp)) {
        return false;
    }
    const nextAllowedDownloadTime =
        lastDownloadTimestamp * 1000 + cooldownDays * 24 * 60 * 60 * 1000;
    return nextAllowedDownloadTime > nowMs;
}
