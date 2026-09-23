export interface DailyAuthor {
    userId: string;
    userName: string;
}

export interface DailyAuthorBatchSummary {
    status: 'completed' | 'completed_with_errors';
    total: number;
    completed: number;
    failed: number;
    timed_out: number;
}

interface DailyAuthorBatchOptions<T extends DailyAuthor, TResult> {
    authorTimeoutMs: number;
    runAuthor: (author: T, signal: AbortSignal) => Promise<TResult>;
    afterAuthor?: (author: T, result: TResult) => Promise<void>;
    logError?: (message: string) => void;
}

class DailyAuthorTimeoutError extends Error {
    constructor(
        readonly authorId: string,
        readonly timeoutMs: number
    ) {
        super(`author ${authorId} did not settle within ${timeoutMs}ms`);
        this.name = 'DailyAuthorTimeoutError';
    }
}

export async function runDailyAuthorBatch<T extends DailyAuthor, TResult = void>(
    authors: readonly T[],
    options: DailyAuthorBatchOptions<T, TResult>
): Promise<DailyAuthorBatchSummary> {
    const counts = {
        total: authors.length,
        completed: 0,
        failed: 0,
        timed_out: 0,
    };
    const logError = options.logError ?? ((message: string) => console.error(message));

    for (const author of authors) {
        try {
            const result = await runAuthorWithTimeout(
                author,
                options.authorTimeoutMs,
                options.runAuthor
            );
            await options.afterAuthor?.(author, result);
            counts.completed++;
        } catch (error) {
            const timedOut = error instanceof DailyAuthorTimeoutError;
            if (timedOut) {
                counts.timed_out++;
            } else {
                counts.failed++;
            }
            logError(
                `daily_download_author_failed ${JSON.stringify({
                    author_id: author.userId,
                    author_name: author.userName,
                    status: timedOut ? 'timed_out' : 'failed',
                    ...(timedOut ? { timeout_ms: options.authorTimeoutMs } : {}),
                    error: formatError(error),
                })}`
            );
        }
    }

    return {
        status:
            counts.failed > 0 || counts.timed_out > 0
                ? 'completed_with_errors'
                : 'completed',
        ...counts,
    };
}

export function assertDailyAuthorBatchSucceeded(
    summary: DailyAuthorBatchSummary
): void {
    if (summary.status === 'completed_with_errors') {
        throw new Error(`daily author batch incomplete: ${JSON.stringify(summary)}`);
    }
}

async function runAuthorWithTimeout<T extends DailyAuthor, TResult>(
    author: T,
    timeoutMs: number,
    runAuthor: (author: T, signal: AbortSignal) => Promise<TResult>
): Promise<TResult> {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const controller = new AbortController();
    try {
        return await Promise.race([
            Promise.resolve().then(() => runAuthor(author, controller.signal)),
            new Promise<never>((_, reject) => {
                timer = setTimeout(() => {
                    const timeoutError = new DailyAuthorTimeoutError(
                        author.userId,
                        timeoutMs
                    );
                    controller.abort(timeoutError);
                    reject(timeoutError);
                }, timeoutMs);
            }),
        ]);
    } finally {
        if (timer !== undefined) {
            clearTimeout(timer);
        }
    }
}

function formatError(error: unknown): string {
    return error instanceof Error ? error.message : String(error);
}
