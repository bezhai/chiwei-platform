import { AsyncLocalStorage } from 'node:async_hooks';
import {
    defaultHttpInstance,
    type HttpInstance,
    type HttpRequestOptions,
} from '@larksuiteoapi/node-sdk';

/** One budget for token acquisition and every page of a directory operation. */
export const DIRECTORY_REQUEST_TIMEOUT_MS = 15_000;
const requestScope = new AsyncLocalStorage<{
    controller: AbortController;
    expiresAt: number;
    timeoutMs: number;
}>();

export function throwIfDirectoryDeadlineExpired(): void {
    const scope = requestScope.getStore();
    if (!scope) return;
    if (Date.now() >= scope.expiresAt && !scope.controller.signal.aborted) {
        scope.controller.abort(
            new Error(
                `lark directory request timed out after ${scope.timeoutMs}ms`,
            ),
        );
    }
    scope.controller.signal.throwIfAborted();
}

export async function withDirectoryDeadline<T>(
    run: () => Promise<T>,
    timeoutMs = DIRECTORY_REQUEST_TIMEOUT_MS,
): Promise<T> {
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0)
        throw new Error('directory deadline must be positive and finite');
    const controller = new AbortController();
    const timer = setTimeout(
        () =>
            controller.abort(
                new Error(
                    `lark directory request timed out after ${timeoutMs}ms`,
                ),
            ),
        timeoutMs,
    );
    try {
        const result = await requestScope.run(
            { controller, expiresAt: Date.now() + timeoutMs, timeoutMs },
            run,
        );
        controller.signal.throwIfAborted();
        return result;
    } catch (error) {
        // The SDK catches token HTTP errors and can throw a secondary destructuring
        // error. Preserve the deadline as the cause visible to the directory caller.
        controller.signal.throwIfAborted();
        throw error;
    } finally {
        clearTimeout(timer);
    }
}

function options<D>(
    config?: HttpRequestOptions<D>,
): HttpRequestOptions<D> & { signal?: AbortSignal } {
    const signal = requestScope.getStore()?.controller.signal;
    if (!signal) return config ?? {};
    throwIfDirectoryDeadlineExpired();
    return { ...config, signal };
}

/**
 * SDK token requests use post(), while resource requests use request(). Wrap every
 * method without modifying the shared Axios instance or its response interceptors.
 * Outside a directory scope the original options and response pass through.
 */
export function createDirectoryHttpTransport(
    base: HttpInstance = defaultHttpInstance,
): HttpInstance {
    async function invoke<T>(run: () => Promise<T>): Promise<T> {
        try {
            return await run();
        } catch (error) {
            // Axios cancellation errors include request configuration, which may
            // contain an app secret or token. Do not pass that object to SDK loggers.
            requestScope.getStore()?.controller.signal.throwIfAborted();
            throw error;
        }
    }
    return {
        request: (config) => invoke(() => base.request(options(config))),
        get: (url, config) =>
            invoke(() =>
                base.get(
                    url,
                    requestScope.getStore() ? options(config) : config,
                ),
            ),
        delete: (url, config) =>
            invoke(() =>
                base.delete(
                    url,
                    requestScope.getStore() ? options(config) : config,
                ),
            ),
        head: (url, config) =>
            invoke(() =>
                base.head(
                    url,
                    requestScope.getStore() ? options(config) : config,
                ),
            ),
        options: (url, config) =>
            invoke(() =>
                base.options(
                    url,
                    requestScope.getStore() ? options(config) : config,
                ),
            ),
        post: (url, data, config) =>
            invoke(() =>
                base.post(
                    url,
                    data,
                    requestScope.getStore() ? options(config) : config,
                ),
            ),
        put: (url, data, config) =>
            invoke(() =>
                base.put(
                    url,
                    data,
                    requestScope.getStore() ? options(config) : config,
                ),
            ),
        patch: (url, data, config) =>
            invoke(() =>
                base.patch(
                    url,
                    data,
                    requestScope.getStore() ? options(config) : config,
                ),
            ),
    };
}
