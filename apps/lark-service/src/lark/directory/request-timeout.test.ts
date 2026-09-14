import { describe, expect, it } from 'bun:test';
import { createServer, type Server } from 'node:http';
import { defaultHttpInstance } from '@larksuiteoapi/node-sdk';
import {
    createDirectoryHttpTransport,
    withDirectoryDeadline,
} from './request-timeout';

async function listen(server: Server): Promise<string> {
    await new Promise<void>((resolve) =>
        server.listen(0, '127.0.0.1', resolve),
    );
    return `http://127.0.0.1:${(server.address() as { port: number }).port}`;
}

describe('directory HTTP deadline', () => {
    it('aborts an actual HTTP request that never responds, closing its socket', async () => {
        let closed = false;
        let entered = false;
        const server = createServer((req) => {
            entered = true;
            req.on('close', () => {
                closed = true;
            });
        });
        const url = await listen(server);
        try {
            const transport = createDirectoryHttpTransport();
            await expect(
                withDirectoryDeadline(() => transport.get(url), 80),
            ).rejects.toThrow('directory request timed out');
            for (let i = 0; i < 20 && !closed; i++) await Bun.sleep(10);
            expect(entered).toBe(true);
            expect(closed).toBe(true);
        } finally {
            server.closeAllConnections();
            server.close();
        }
    });

    it('preserves SDK response unwrapping and response-header behavior', async () => {
        const server = createServer((_req, res) => {
            res.setHeader('Content-Type', 'application/json');
            res.setHeader('X-Test', 'ok');
            res.end('{"code":0,"data":{"name":"真人"}}');
        });
        const url = await listen(server);
        try {
            const transport = createDirectoryHttpTransport(defaultHttpInstance);
            expect(
                await withDirectoryDeadline(
                    () => transport.get<unknown>(url),
                    500,
                ),
            ).toEqual({ code: 0, data: { name: '真人' } });
            const result = await withDirectoryDeadline(
                () =>
                    transport.get<{
                        data: unknown;
                        headers: Record<string, string>;
                    }>(url, { $return_headers: true }),
                500,
            );
            expect(result.data).toEqual({ code: 0, data: { name: '真人' } });
            expect(result.headers['x-test']).toBe('ok');
        } finally {
            server.closeAllConnections();
            server.close();
        }
    });

    it('isolates concurrent scopes and leaves unrelated requests without a deadline signal', async () => {
        const signals: Array<AbortSignal | undefined> = [];
        const base = {
            request: async (opts: { signal?: AbortSignal }) => {
                signals.push(opts.signal);
                return 'ok';
            },
        };
        const transport = createDirectoryHttpTransport(base as never);
        await Promise.all([
            withDirectoryDeadline(async () => {
                await Bun.sleep(5);
                return transport.request({});
            }, 200),
            withDirectoryDeadline(() => transport.request({}), 200),
            transport.request({}),
        ]);
        expect(signals.filter(Boolean)).toHaveLength(2);
        expect(new Set(signals.filter(Boolean)).size).toBe(2);
        expect(signals.filter((signal) => signal === undefined)).toHaveLength(
            1,
        );
        expect(
            signals.filter(Boolean).every((signal) => !signal!.aborted),
        ).toBe(true);
    });

    it('passes a scoped signal through all eight SDK HTTP methods', async () => {
        const captured: Array<{ method: string; args: unknown[] }> = [];
        const base = Object.fromEntries(
            [
                'request',
                'get',
                'delete',
                'head',
                'options',
                'post',
                'put',
                'patch',
            ].map((method) => [
                method,
                async (...args: unknown[]) => {
                    captured.push({ method, args });
                    return 'ok';
                },
            ]),
        );
        const transport = createDirectoryHttpTransport(base as never);
        await withDirectoryDeadline(async () => {
            await transport.request({ url: '/request' });
            await transport.get('/get');
            await transport.delete('/delete');
            await transport.head('/head');
            await transport.options('/options');
            await transport.post('/post', { x: 1 });
            await transport.put('/put', { x: 2 });
            await transport.patch('/patch', { x: 3 });
        }, 200);
        expect(captured).toHaveLength(8);
        const signals = captured.map(
            ({ args }) =>
                (args[args.length - 1] as { signal: AbortSignal }).signal,
        );
        expect(signals.every((signal) => signal instanceof AbortSignal)).toBe(
            true,
        );
        expect(new Set(signals).size).toBe(1);
        expect(captured[5]!.args[1]).toEqual({ x: 1 });
    });

    it('converts cancellation errors to a plain error before the SDK can log request credentials', async () => {
        let delivered: unknown;
        const base = {
            post: async (
                _url: string,
                _data: unknown,
                opts: { signal: AbortSignal },
            ) =>
                new Promise((_resolve, reject) => {
                    opts.signal.addEventListener(
                        'abort',
                        () =>
                            reject(
                                Object.assign(new Error('canceled'), {
                                    config: {
                                        data: { app_secret: 'test-secret' },
                                        headers: {
                                            Authorization: 'test-token',
                                        },
                                    },
                                }),
                            ),
                        { once: true },
                    );
                }),
        };
        const transport = createDirectoryHttpTransport(base as never);
        await expect(
            withDirectoryDeadline(
                () =>
                    transport.post('/token').catch((error) => {
                        delivered = error;
                        throw error;
                    }),
                20,
            ),
        ).rejects.toThrow('directory request timed out');
        expect(delivered).toBeInstanceOf(Error);
        expect(delivered).not.toHaveProperty('config');
        expect(JSON.stringify(delivered)).not.toContain('test-secret');
    });
});
