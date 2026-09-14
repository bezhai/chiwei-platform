import { describe, expect, it } from 'bun:test';
import { createSdkLarkDirectoryApi } from './sdk-directory-api';
import type { LarkClientPool } from '../outbound/sdk-lark-api';
import { LarkClient } from '@inner/lark-utils';
import axios from 'axios';
import {
    createServer,
    type IncomingMessage,
    type ServerResponse,
} from 'node:http';
import { createDirectoryHttpTransport } from './request-timeout';

function rig(pages: unknown[]) {
    const calls: unknown[][] = [];
    const api = createSdkLarkDirectoryApi({
        current: () => ({
            getChatMembers: async (...args: unknown[]) => {
                calls.push(args);
                const page = pages.shift();
                if (page instanceof Error) throw page;
                return page;
            },
            getUserInfo: async (id: string, type: string) => {
                calls.push([id, type]);
                return {
                    user: { union_id: id, name: '张若', open_id: 'ou_1' },
                };
            },
        }),
    } as unknown as LarkClientPool);
    return { api, calls };
}
const item = (id: string, name = '张若') => ({
    member_id_type: 'union_id',
    member_id: id,
    name,
});

describe('SDK directory API', () => {
    it('follows tokens through short and empty pages using union IDs', async () => {
        const h = rig([
            { items: [], has_more: true, page_token: 'next' },
            { items: [item('on_1')], has_more: false },
        ]);
        expect(await h.api.members('oc_1')).toEqual([
            { unionId: 'on_1', name: '张若' },
        ]);
        expect(h.calls).toEqual([
            ['oc_1', undefined, 'union_id'],
            ['oc_1', 'next', 'union_id'],
        ]);
    });
    for (const [label, page] of [
        ['missing data', undefined],
        ['missing items', { has_more: false }],
        ['missing has_more', { items: [] }],
        [
            'wrong ID type',
            {
                items: [{ ...item('on_1'), member_id_type: 'open_id' }],
                has_more: false,
            },
        ],
        ['missing name', { items: [item('on_1', '')], has_more: false }],
        ['missing cursor', { items: [], has_more: true }],
    ] as const) {
        it(`rejects ${label} before any snapshot is handed out`, async () => {
            await expect(rig([page]).api.members('oc_1')).rejects.toThrow();
        });
    }
    it('rejects repeated cursors and conflicting duplicate users', async () => {
        await expect(
            rig([
                { items: [], has_more: true, page_token: 'x' },
                { items: [], has_more: true, page_token: 'x' },
            ]).api.members('oc_1'),
        ).rejects.toThrow('cursor');
        await expect(
            rig([
                {
                    items: [item('on_1'), item('on_1', '另一个名字')],
                    has_more: false,
                },
            ]).api.members('oc_1'),
        ).rejects.toThrow('duplicate');
    });
    it('propagates API failure after a successful page', async () => {
        await expect(
            rig([
                { items: [item('on_1')], has_more: true, page_token: 'next' },
                new Error('no permission'),
            ]).api.members('oc_1'),
        ).rejects.toThrow('no permission');
    });
    it('gets a real profile by union ID', async () => {
        const h = rig([]);
        expect(await h.api.user('on_1')).toEqual({
            unionId: 'on_1',
            name: '张若',
            openId: 'ou_1',
        });
        expect(h.calls).toEqual([['on_1', 'union_id']]);
    });
});

async function actualSdk(
    handler: (req: IncomingMessage, res: ServerResponse) => void,
    timeoutMs: number,
) {
    const server = createServer(handler);
    await new Promise<void>((resolve) =>
        server.listen(0, '127.0.0.1', resolve),
    );
    const local = `http://127.0.0.1:${(server.address() as { port: number }).port}`;
    const signals: unknown[] = [];
    const originalUrls: string[] = [];
    const http = axios.create({
        adapter: async (config) => {
            originalUrls.push(config.url!);
            signals.push(config.signal);
            const path = config.url!.includes('/auth/')
                ? '/token'
                : config.url!.includes('/contact/')
                  ? '/user'
                  : config.params?.page_token
                    ? '/last'
                    : '/first';
            return axios.getAdapter('http')({
                ...config,
                url: local + path,
                proxy: false,
            });
        },
    });
    // Match the documented defaultHttpInstance response contract. Requests still
    // use the real Axios HTTP adapter and the real SDK token and resource paths.
    http.interceptors.response.use((response) => response.data);
    const client = new LarkClient({
        appId: `cli_deadline_${crypto.randomUUID()}`,
        appSecret: 'test-only-secret',
        httpInstance: createDirectoryHttpTransport(http),
    });
    return {
        api: createSdkLarkDirectoryApi({ current: () => client }, timeoutMs),
        signals,
        originalUrls,
        close() {
            server.closeAllConnections();
            server.close();
        },
    };
}

describe('directory deadline through the real SDK and token manager', () => {
    it('cancels a hanging token request, before any member request', async () => {
        let closed = false;
        const h = await actualSdk(
            (req) =>
                req.on('close', () => {
                    closed = true;
                }),
            80,
        );
        try {
            await expect(h.api.members('oc_1')).rejects.toThrow(
                'directory request timed out',
            );
            for (let i = 0; i < 20 && !closed; i++) await Bun.sleep(10);
            expect(closed).toBe(true);
            expect(h.originalUrls).toHaveLength(1);
            expect(h.originalUrls[0]).toContain(
                '/auth/v3/tenant_access_token/internal',
            );
            expect((h.signals[0] as AbortSignal).aborted).toBe(true);
        } finally {
            h.close();
        }
    });

    it('uses one total deadline across token acquisition and multiple member pages', async () => {
        let canceledLast = false;
        const h = await actualSdk((req, res) => {
            res.setHeader('Content-Type', 'application/json');
            const path = req.url!.split('?')[0];
            const body =
                path === '/token'
                    ? {
                          code: 0,
                          tenant_access_token: 'test-token',
                          expire: 7200,
                      }
                    : path === '/first'
                      ? {
                            code: 0,
                            data: {
                                items: [item('on_1')],
                                has_more: true,
                                page_token: 'last',
                            },
                        }
                      : {
                            code: 0,
                            data: { items: [item('on_2')], has_more: false },
                        };
            if (path === '/last')
                req.on('close', () => {
                    canceledLast = !res.writableEnded;
                });
            setTimeout(() => res.end(JSON.stringify(body)), 90);
        }, 250);
        try {
            await expect(h.api.members('oc_1')).rejects.toThrow(
                'directory request timed out',
            );
            for (let i = 0; i < 20 && !canceledLast; i++) await Bun.sleep(10);
            expect(canceledLast).toBe(true);
            expect(h.signals).toHaveLength(3);
            expect(new Set(h.signals).size).toBe(1);
            expect((h.signals[0] as AbortSignal).aborted).toBe(true);
        } finally {
            h.close();
        }
    });

    it('bounds a hanging user-profile response after successfully acquiring a token', async () => {
        let canceledUser = false;
        const h = await actualSdk((req, res) => {
            if (req.url === '/token') {
                res.setHeader('Content-Type', 'application/json');
                res.end(
                    JSON.stringify({
                        code: 0,
                        tenant_access_token: 'test-token',
                        expire: 7200,
                    }),
                );
            } else
                req.on('close', () => {
                    canceledUser = true;
                });
        }, 80);
        try {
            await expect(h.api.user('on_1')).rejects.toThrow(
                'directory request timed out',
            );
            for (let i = 0; i < 20 && !canceledUser; i++) await Bun.sleep(10);
            expect(canceledUser).toBe(true);
            expect(h.originalUrls).toHaveLength(2);
            expect(h.originalUrls[1]).toContain('/contact/v3/users/on_1');
        } finally {
            h.close();
        }
    });
});
