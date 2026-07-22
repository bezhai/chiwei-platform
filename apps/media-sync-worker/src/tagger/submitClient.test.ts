import { describe, expect, it } from 'bun:test';
import {
    TaggerSubmitClient,
    TaggerSubmitError,
    TaggerTaskNotFoundError,
    type FetchLike,
} from './submitClient';

function jsonResponse(body: unknown, init?: ResponseInit): Response {
    return new Response(JSON.stringify(body), {
        status: init?.status ?? 200,
        headers: { 'content-type': 'application/json' },
    });
}

describe('TaggerSubmitClient', () => {
    it('submits paths and callback URL to tagger entry with bearer auth', async () => {
        const calls: Array<{ url: string; init: RequestInit }> = [];
        const fetchImpl: FetchLike = async (url, init = {}) => {
            calls.push({ url: String(url), init });
            return jsonResponse({ task_id: 'task-1', status: 'accepted' });
        };

        const client = new TaggerSubmitClient(
            {
                entryUrl: 'http://tagger-entry:8000/',
                apiToken: 'caller-token',
                timeoutMs: 10000,
                retries: 0,
            },
            fetchImpl
        );

        const result = await client.submit({
            paths: ['100363338_p1.jpg'],
            callbackUrl: 'http://media-sync-worker/internal/tagger/callback',
        });

        expect(result).toEqual({ taskId: 'task-1', status: 'accepted' });
        expect(calls.length).toBe(1);
        expect(calls[0].url).toBe('http://tagger-entry:8000/api/v1/tagger/submit');
        expect(calls[0].init.method).toBe('POST');
        expect(calls[0].init.headers).toEqual({
            authorization: 'Bearer caller-token',
            'content-type': 'application/json',
        });
        expect(JSON.parse(String(calls[0].init.body))).toEqual({
            paths: ['100363338_p1.jpg'],
            callback_url: 'http://media-sync-worker/internal/tagger/callback',
        });
    });

    it('does not retry caller errors', async () => {
        let calls = 0;
        const fetchImpl: FetchLike = async () => {
            calls++;
            return jsonResponse({ detail: 'bad callback_url' }, { status: 400 });
        };
        const client = new TaggerSubmitClient(
            { entryUrl: 'http://tagger-entry:8000', apiToken: 'token', timeoutMs: 10000, retries: 3 },
            fetchImpl
        );

        await expect(
            client.submit({ paths: ['a.jpg'], callbackUrl: 'http://callback' })
        ).rejects.toThrow(TaggerSubmitError);
        expect(calls).toBe(1);
    });

    it('retries transient server errors', async () => {
        let calls = 0;
        const fetchImpl: FetchLike = async () => {
            calls++;
            if (calls === 1) {
                return jsonResponse({ detail: 'busy' }, { status: 503 });
            }
            return jsonResponse({ task_id: 'task-2', status: 'accepted' });
        };
        const client = new TaggerSubmitClient(
            { entryUrl: 'http://tagger-entry:8000', apiToken: 'token', timeoutMs: 10000, retries: 2 },
            fetchImpl
        );

        const result = await client.submit({ paths: ['a.jpg'], callbackUrl: 'http://callback' });

        expect(result.taskId).toBe('task-2');
        expect(calls).toBe(2);
    });

    it('rejects malformed success responses', async () => {
        const fetchImpl: FetchLike = async () => jsonResponse({ status: 'accepted' });
        const client = new TaggerSubmitClient(
            { entryUrl: 'http://tagger-entry:8000', apiToken: 'token', timeoutMs: 10000, retries: 0 },
            fetchImpl
        );

        await expect(
            client.submit({ paths: ['a.jpg'], callbackUrl: 'http://callback' })
        ).rejects.toThrow('tagger submit response task_id must be a string');
    });

    it('rejects a submit status outside the current accepted contract', async () => {
        const client = new TaggerSubmitClient(
            { entryUrl: 'http://tagger-entry:8000', apiToken: 'token', timeoutMs: 10000, retries: 0 },
            async () => jsonResponse({ task_id: 'task-1', status: 'queued' }),
        );

        await expect(
            client.submit({ paths: ['a.jpg'], callbackUrl: 'http://callback' })
        ).rejects.toThrow('tagger submit response status must be accepted');
    });

    it('gets the current remote task with bearer auth and preserves its result payload', async () => {
        const calls: Array<{ url: string; init: RequestInit }> = [];
        const resultPayload = {
            task_id: 'task-1',
            status: 'completed',
            rows: [{ id: 'a.jpg', schema_version: 1, future_capability: { value: 'kept' } }],
            dups: [],
        };
        const fetchImpl: FetchLike = async (url, init = {}) => {
            calls.push({ url: String(url), init });
            return jsonResponse({
                task_id: 'task-1',
                status: 'completed',
                paths: ['a.jpg'],
                result: resultPayload,
                attempts: 0,
                error: null,
            });
        };
        const client = new TaggerSubmitClient(
            { entryUrl: 'http://tagger-entry:8000/', apiToken: 'token', timeoutMs: 10000, retries: 0 },
            fetchImpl,
        );

        const result = await client.getTask('task-1');

        expect(result).toEqual({
            taskId: 'task-1',
            status: 'completed',
            paths: ['a.jpg'],
            result: resultPayload,
            error: null,
        });
        expect(calls).toHaveLength(1);
        expect(calls[0].url).toBe('http://tagger-entry:8000/api/v1/tagger/tasks/task-1');
        expect(calls[0].init.method).toBe('GET');
        expect(calls[0].init.headers).toEqual({ authorization: 'Bearer token' });
    });

    it.each(['accepted', 'running', 'pending_callback', 'completed', 'failed'] as const)(
        'accepts the explicit remote task status %s',
        async (status) => {
            const client = new TaggerSubmitClient(
                { entryUrl: 'http://tagger-entry:8000', apiToken: 'token', timeoutMs: 10000, retries: 0 },
                async () => jsonResponse({ task_id: 'task-1', status, paths: ['a.jpg'], result: null, error: null }),
            );

            expect((await client.getTask('task-1')).status).toBe(status);
        },
    );

    it('maps a 404 task lookup to an explicit not-found error', async () => {
        const client = new TaggerSubmitClient(
            { entryUrl: 'http://tagger-entry:8000', apiToken: 'token', timeoutMs: 10000, retries: 0 },
            async () => jsonResponse({ detail: 'task not found' }, { status: 404 }),
        );

        await expect(client.getTask('missing')).rejects.toBeInstanceOf(TaggerTaskNotFoundError);
    });

    it('rejects unknown remote task status instead of guessing an alias', async () => {
        const client = new TaggerSubmitClient(
            { entryUrl: 'http://tagger-entry:8000', apiToken: 'token', timeoutMs: 10000, retries: 0 },
            async () => jsonResponse({ task_id: 'task-1', status: 'DONE', paths: ['a.jpg'], result: null }),
        );

        await expect(client.getTask('task-1')).rejects.toThrow('unknown tagger task status: DONE');
    });

    it.each([
        {
            body: { task_id: 'task-1', status: 'running', paths: [], result: null, error: null },
            error: 'paths must be a non-empty array',
        },
        {
            body: { task_id: 'task-1', status: 'running', paths: ['a.jpg'], error: null },
            error: 'result must be an object or null',
        },
        {
            body: { task_id: 'task-1', status: 'running', paths: ['a.jpg'], result: null },
            error: 'error must be a string or null',
        },
    ])('rejects malformed current task responses: $error', async ({ body, error }) => {
        const client = new TaggerSubmitClient(
            { entryUrl: 'http://tagger-entry:8000', apiToken: 'token', timeoutMs: 10000, retries: 0 },
            async () => jsonResponse(body),
        );

        await expect(client.getTask('task-1')).rejects.toThrow(error);
    });
});
