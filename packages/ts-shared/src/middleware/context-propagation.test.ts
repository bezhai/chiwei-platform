import { describe, test, expect } from 'bun:test';
import { Hono } from 'hono';
import { createContextPropagationMiddleware, getContextHeaders } from './context-propagation';
import { asyncLocalStorage } from './context';

describe('contextPropagationMiddleware', () => {
    const middleware = createContextPropagationMiddleware();

    test('extracts x-ctx-* headers into AsyncLocalStorage', async () => {
        const app = new Hono();
        app.use('*', middleware);
        app.get('/test', (c) => {
            const store = asyncLocalStorage.getStore();
            return c.json({
                lane: store?.['ctx:lane'],
                gray: store?.['ctx:gray-group'],
            });
        });

        const res = await app.request('/test', {
            headers: {
                'x-ctx-lane': 'feat-test',
                'x-ctx-gray-group': 'beta',
                'x-unrelated': 'ignored',
            },
        });

        const body = await res.json();
        expect(body.lane).toBe('feat-test');
        expect(body.gray).toBe('beta');
    });

    test('getContextHeaders returns all x-ctx-* values from store', async () => {
        const app = new Hono();
        app.use('*', middleware);
        app.get('/test', (c) => {
            const headers = getContextHeaders();
            return c.json(headers);
        });

        const res = await app.request('/test', {
            headers: {
                'x-ctx-lane': 'dev',
                'x-ctx-trace-id': 'abc-123',
            },
        });

        const body = await res.json();
        expect(body['x-ctx-lane']).toBe('dev');
        expect(body['x-ctx-trace-id']).toBe('abc-123');
    });

    test('works with no x-ctx-* headers', async () => {
        const app = new Hono();
        app.use('*', middleware);
        app.get('/test', (c) => {
            return c.json(getContextHeaders());
        });

        const res = await app.request('/test');
        const body = await res.json();
        expect(Object.keys(body).length).toBe(0);
    });
});
