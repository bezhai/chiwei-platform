import { describe, expect, it } from 'bun:test';
import { createTaggerCallbackHandler, type TaggerCallbackRepository } from './callbackServer';
import type { TaggerCallbackPayload } from './types';

class FakeRepo implements TaggerCallbackRepository {
    payloads: TaggerCallbackPayload[] = [];

    async applyCallback(payload: TaggerCallbackPayload): Promise<void> {
        this.payloads.push(payload);
    }
}

function request(path: string, init?: RequestInit): Request {
    return new Request(`http://worker${path}`, init);
}

describe('createTaggerCallbackHandler', () => {
    it('serves health without auth', async () => {
        const repo = new FakeRepo();
        const handler = createTaggerCallbackHandler(repo, { authToken: 'callback-token' });

        const res = await handler(request('/health'));

        expect(res.status).toBe(200);
        expect(await res.json()).toEqual({ status: 'ok' });
    });

    it('rejects callback without bearer auth', async () => {
        const repo = new FakeRepo();
        const handler = createTaggerCallbackHandler(repo, { authToken: 'callback-token' });

        const res = await handler(
            request('/internal/tagger/callback', {
                method: 'POST',
                body: JSON.stringify({ task_id: 'task-1', status: 'completed', rows: [] }),
            })
        );

        expect(res.status).toBe(401);
        expect(repo.payloads).toEqual([]);
    });

    it('rejects invalid callback payloads', async () => {
        const repo = new FakeRepo();
        const handler = createTaggerCallbackHandler(repo, { authToken: 'callback-token' });

        const res = await handler(
            request('/internal/tagger/callback', {
                method: 'POST',
                headers: { authorization: 'Bearer callback-token' },
                body: JSON.stringify({ task_id: 'task-1', status: 'completed', rows: [{ id: '' }] }),
            })
        );

        expect(res.status).toBe(400);
        expect(repo.payloads).toEqual([]);
    });

    it('stores valid callback payloads', async () => {
        const repo = new FakeRepo();
        const handler = createTaggerCallbackHandler(repo, { authToken: 'callback-token' });
        const payload = {
            task_id: 'task-1',
            status: 'completed',
            rows: [{ id: '100363338_p1.jpg', schema_version: 1 }],
            dups: [],
        };

        const res = await handler(
            request('/internal/tagger/callback', {
                method: 'POST',
                headers: { authorization: 'Bearer callback-token' },
                body: JSON.stringify(payload),
            })
        );

        expect(res.status).toBe(200);
        expect(await res.json()).toEqual({ status: 'ok' });
        expect(repo.payloads).toEqual([payload]);
    });

    it('returns 404 for unknown routes', async () => {
        const repo = new FakeRepo();
        const handler = createTaggerCallbackHandler(repo, { authToken: 'callback-token' });

        const res = await handler(request('/missing'));

        expect(res.status).toBe(404);
    });
});
