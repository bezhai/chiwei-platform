export type FetchLike = (url: string, init?: RequestInit) => Promise<Response>;

export interface TaggerSubmitClientConfig {
    entryUrl: string;
    apiToken: string;
    timeoutMs: number;
    retries: number;
}

export interface TaggerSubmitRequest {
    paths: string[];
    callbackUrl: string;
}

export interface TaggerSubmitResult {
    taskId: string;
    status: 'accepted';
}

export type RemoteTaggerTaskStatus =
    | 'accepted'
    | 'running'
    | 'pending_callback'
    | 'completed'
    | 'failed';

export interface RemoteTaggerTask {
    taskId: string;
    status: RemoteTaggerTaskStatus;
    paths: string[];
    result: Record<string, unknown> | null;
    error: string | null;
}

export class TaggerSubmitError extends Error {
    constructor(
        message: string,
        readonly status?: number,
        readonly responseBody?: string
    ) {
        super(message);
        this.name = 'TaggerSubmitError';
    }
}

export class TaggerTaskNotFoundError extends Error {
    constructor(readonly taskId: string) {
        super(`tagger task not found: ${taskId}`);
        this.name = 'TaggerTaskNotFoundError';
    }
}

const REMOTE_TASK_STATUSES = new Set<RemoteTaggerTaskStatus>([
    'accepted',
    'running',
    'pending_callback',
    'completed',
    'failed',
]);

export class TaggerSubmitClient {
    constructor(
        private readonly config: TaggerSubmitClientConfig,
        private readonly fetchImpl: FetchLike = fetch
    ) {}

    async submit(req: TaggerSubmitRequest): Promise<TaggerSubmitResult> {
        let lastError: unknown;

        for (let attempt = 0; attempt <= this.config.retries; attempt++) {
            try {
                return await this.submitOnce(req);
            } catch (err) {
                lastError = err;
                if (!this.shouldRetry(err, attempt)) {
                    throw err;
                }
                console.warn(
                    `Tagger submit retrying: paths=${req.paths.join(',')} attempt=${attempt + 1}/${this.config.retries + 1} error=${formatError(err)}`
                );
            }
        }

        throw lastError;
    }

    async getTask(taskId: string): Promise<RemoteTaggerTask> {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), this.config.timeoutMs);

        try {
            const response = await this.fetchImpl(
                `${this.config.entryUrl.replace(/\/+$/, '')}/api/v1/tagger/tasks/${encodeURIComponent(taskId)}`,
                {
                    method: 'GET',
                    headers: { authorization: `Bearer ${this.config.apiToken}` },
                    signal: controller.signal,
                },
            );
            if (response.status === 404) {
                throw new TaggerTaskNotFoundError(taskId);
            }
            if (!response.ok) {
                const body = await response.text();
                throw new TaggerSubmitError(
                    `tagger task lookup failed with HTTP ${response.status}`,
                    response.status,
                    body,
                );
            }

            return parseRemoteTask(await response.json(), taskId);
        } finally {
            clearTimeout(timer);
        }
    }

    private async submitOnce(req: TaggerSubmitRequest): Promise<TaggerSubmitResult> {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), this.config.timeoutMs);

        try {
            const response = await this.fetchImpl(this.submitUrl(), {
                method: 'POST',
                headers: {
                    authorization: `Bearer ${this.config.apiToken}`,
                    'content-type': 'application/json',
                },
                body: JSON.stringify({
                    paths: req.paths,
                    callback_url: req.callbackUrl,
                }),
                signal: controller.signal,
            });

            if (!response.ok) {
                const body = await response.text();
                throw new TaggerSubmitError(
                    `tagger submit failed with HTTP ${response.status}`,
                    response.status,
                    body
                );
            }

            const data = await response.json();
            if (typeof data.task_id !== 'string' || data.task_id === '') {
                throw new Error('tagger submit response task_id must be a string');
            }
            if (data.status !== 'accepted') {
                throw new Error(`tagger submit response status must be accepted: ${String(data.status)}`);
            }
            return {
                taskId: data.task_id,
                status: 'accepted',
            };
        } finally {
            clearTimeout(timer);
        }
    }

    private submitUrl(): string {
        return `${this.config.entryUrl.replace(/\/+$/, '')}/api/v1/tagger/submit`;
    }

    private shouldRetry(err: unknown, attempt: number): boolean {
        if (attempt >= this.config.retries) {
            return false;
        }
        if (err instanceof TaggerSubmitError) {
            return err.status === undefined || err.status >= 500;
        }
        return true;
    }
}

function parseRemoteTask(value: unknown, expectedTaskId: string): RemoteTaggerTask {
    if (!isRecord(value)) {
        throw new Error('tagger task response must be an object');
    }
    if (value.task_id !== expectedTaskId) {
        throw new Error('tagger task response task_id mismatch');
    }
    if (typeof value.status !== 'string' || !REMOTE_TASK_STATUSES.has(value.status as RemoteTaggerTaskStatus)) {
        throw new Error(`unknown tagger task status: ${String(value.status)}`);
    }
    if (
        !Array.isArray(value.paths)
        || value.paths.length === 0
        || value.paths.some((path) => typeof path !== 'string' || path === '')
    ) {
        throw new Error('tagger task response paths must be a non-empty array of non-empty strings');
    }
    if (!Object.hasOwn(value, 'result') || (value.result !== null && !isRecord(value.result))) {
        throw new Error('tagger task response result must be an object or null');
    }
    if (!Object.hasOwn(value, 'error') || (value.error !== null && typeof value.error !== 'string')) {
        throw new Error('tagger task response error must be a string or null');
    }

    return {
        taskId: expectedTaskId,
        status: value.status as RemoteTaggerTaskStatus,
        paths: value.paths,
        result: value.result as Record<string, unknown> | null,
        error: value.error as string | null,
    };
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function formatError(err: unknown): string {
    if (err instanceof TaggerSubmitError) {
        return `${err.name}: ${err.message}${err.status ? ` status=${err.status}` : ''}`;
    }
    return err instanceof Error ? `${err.name}: ${err.message}` : String(err);
}
