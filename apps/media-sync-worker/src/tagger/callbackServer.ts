import type { TaggerCallbackPayload } from './types';

export interface TaggerCallbackRepository {
    applyCallback(payload: TaggerCallbackPayload): Promise<void>;
}

export interface TaggerCallbackHandlerConfig {
    authToken: string;
}

export interface TaggerCallbackServerConfig extends TaggerCallbackHandlerConfig {
    port: number;
}

export type RequestHandler = (request: Request) => Promise<Response>;

export function createTaggerCallbackHandler(
    repository: TaggerCallbackRepository,
    config: TaggerCallbackHandlerConfig
): RequestHandler {
    return async (request: Request): Promise<Response> => {
        const url = new URL(request.url);

        if (request.method === 'GET' && url.pathname === '/health') {
            return json({ status: 'ok' });
        }

        if (request.method !== 'POST' || url.pathname !== '/internal/tagger/callback') {
            return json({ error: 'not found' }, 404);
        }

        if (request.headers.get('authorization') !== `Bearer ${config.authToken}`) {
            console.warn(`Tagger callback rejected: reason=unauthorized path=${url.pathname}`);
            return json({ error: 'unauthorized' }, 401);
        }

        let payload: TaggerCallbackPayload;
        try {
            payload = validateTaggerCallbackPayload(await request.json());
        } catch (err) {
            console.warn(`Tagger callback rejected: reason=invalid_payload error=${formatError(err)}`);
            return json({ error: err instanceof Error ? err.message : String(err) }, 400);
        }

        console.log(
            `Tagger callback received: task_id=${payload.task_id} status=${payload.status} rows=${payload.rows.length} dups=${payload.dups?.length ?? 0}`
        );
        try {
            await repository.applyCallback(payload);
        } catch (err) {
            console.error(
                `Tagger callback write failed: task_id=${payload.task_id} status=${payload.status} rows=${payload.rows.length} error=${formatError(err)}`
            );
            return json({ error: 'callback write failed' }, 500);
        }
        console.log(
            `Tagger callback stored: task_id=${payload.task_id} status=${payload.status} rows=${payload.rows.length}`
        );
        return json({ status: 'ok' });
    };
}

export function startTaggerCallbackServer(
    repository: TaggerCallbackRepository,
    config: TaggerCallbackServerConfig
): { stop: () => void } {
    const bun = (globalThis as any).Bun;
    if (!bun?.serve) {
        throw new Error('Bun.serve is required to start tagger callback server');
    }
    const server = bun.serve({
        port: config.port,
        fetch: createTaggerCallbackHandler(repository, config),
    });
    console.log(`Tagger callback server listening on port ${config.port}`);
    return {
        stop: () => server.stop(),
    };
}

export function validateTaggerCallbackPayload(value: unknown): TaggerCallbackPayload {
    if (!isRecord(value)) {
        throw new Error('callback payload must be an object');
    }
    if (typeof value.task_id !== 'string' || value.task_id === '') {
        throw new Error('callback payload task_id must be a non-empty string');
    }
    if (value.status !== 'completed') {
        throw new Error('callback payload status must be completed');
    }
    if (!Array.isArray(value.rows)) {
        throw new Error('callback payload rows must be an array');
    }
    for (const row of value.rows) {
        if (!isRecord(row)) {
            throw new Error('callback row must be an object');
        }
        if (typeof row.id !== 'string' || row.id === '') {
            throw new Error('callback row id must be a non-empty string');
        }
    }
    if (value.dups !== undefined && (!Array.isArray(value.dups) || value.dups.some((dup) => typeof dup !== 'string'))) {
        throw new Error('callback payload dups must be a string array');
    }

    return {
        ...value,
        task_id: value.task_id,
        status: value.status,
        rows: value.rows,
        dups: value.dups,
    } as TaggerCallbackPayload;
}

function isRecord(value: unknown): value is Record<string, unknown> {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function formatError(err: unknown): string {
    return err instanceof Error ? `${err.name}: ${err.message}` : String(err);
}

function json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'content-type': 'application/json' },
    });
}
