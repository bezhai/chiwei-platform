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
    status: string;
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
            }
        }

        throw lastError;
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
            return {
                taskId: data.task_id,
                status: typeof data.status === 'string' ? data.status : 'accepted',
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
