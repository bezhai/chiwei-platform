type Env = Record<string, string | undefined>;

export function positiveTimeout(value: string | undefined, fallback: number, name: string): number {
    if (value === undefined || value === '') return fallback;
    const ms = Number(value);
    if (!Number.isSafeInteger(ms) || ms <= 0 || ms > 2_147_483_647) {
        throw new Error(`${name} must be a positive integer timeout in milliseconds`);
    }
    return ms;
}

export function loadDiscoveryTimeoutMs(env: Env = process.env): number {
    return positiveTimeout(env.DOWNLOAD_DISCOVERY_STEP_TIMEOUT_MS, 180_000, 'DOWNLOAD_DISCOVERY_STEP_TIMEOUT_MS');
}

export type DiscoveryStep = <T>(stage: string, operation: (signal: AbortSignal) => Promise<T>) => Promise<T>;

/** Bound each I/O await, rather than abandoning a whole author that could keep writing. */
export function createDiscoveryStep(
    authorId = 'followers',
    timeoutMs = loadDiscoveryTimeoutMs(),
    log: (message: string) => void = console.info,
): DiscoveryStep {
    return async <T>(stage: string, operation: (signal: AbortSignal) => Promise<T>): Promise<T> => {
        const context = `author=${authorId} stage=${stage}`;
        const started = performance.now();
        let timer: ReturnType<typeof setTimeout> | undefined;
        const controller = new AbortController();
        log(`discovery_step ${context} status=started`);
        try {
            const result = await Promise.race([
                Promise.resolve().then(() => operation(controller.signal)),
                new Promise<never>((_, reject) => {
                    timer = setTimeout(() => {
                        const error = new Error(`${context} timed out after ${timeoutMs}ms`);
                        controller.abort(error);
                        reject(error);
                    }, timeoutMs);
                }),
            ]);
            log(`discovery_step ${context} status=completed ms=${Math.round(performance.now() - started)}`);
            return result;
        } catch (error) {
            log(`discovery_step ${context} status=failed ms=${Math.round(performance.now() - started)} error=${error instanceof Error ? error.message : String(error)}`);
            throw error;
        } finally {
            clearTimeout(timer);
        }
    };
}
