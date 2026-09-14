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

describe('SDK directory API', () => {
    it('gets a real profile by union ID', async () => {
        const calls: unknown[] = [];
        const api = createSdkLarkDirectoryApi({current: () => ({
            getUserInfo: async (id: string, type: string) => {
                calls.push([id, type]);
                return {user: {union_id: id, name: '张若', open_id: 'ou_1'}};
            },
        })} as unknown as LarkClientPool);
        expect(await api.user('on_1')).toEqual({unionId: 'on_1', name: '张若', openId: 'ou_1'});
        expect(calls).toEqual([['on_1', 'union_id']]);
    });
    it('rejects mismatched identities and missing names', async () => {
        for (const user of [{union_id: 'on_other', name: '错误身份'}, {union_id: 'on_1', name: ''}]) {
            const api = createSdkLarkDirectoryApi({current: () => ({getUserInfo: async () => ({user})})} as unknown as LarkClientPool);
            await expect(api.user('on_1')).rejects.toThrow();
        }
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
            const path = config.url!.includes('/auth/') ? '/token' : '/user';
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
    it('cancels a hanging token request, before any profile request', async () => {
        let closed = false;
        const h = await actualSdk(
            (req) =>
                req.on('close', () => {
                    closed = true;
                }),
            80,
        );
        try {
            await expect(h.api.user('on_1')).rejects.toThrow(
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

    it('shares the total deadline between token acquisition and the profile request', async () => {
        let canceledUser = false;
        const h = await actualSdk((req, res) => {
            res.setHeader('Content-Type', 'application/json');
            const isToken = req.url === '/token';
            if (!isToken) req.on('close', () => { canceledUser = !res.writableEnded; });
            setTimeout(() => res.end(JSON.stringify(isToken
                ? {code: 0, tenant_access_token: 'test-token', expire: 7200}
                : {code: 0, data: {user: {union_id: 'on_1', name: '张若'}}})), 100);
        }, 170);
        try {
            await expect(h.api.user('on_1')).rejects.toThrow('directory request timed out');
            for (let i = 0; i < 20 && !canceledUser; i++) await Bun.sleep(10);
            expect(canceledUser).toBe(true);
            expect(h.signals).toHaveLength(2);
            expect(new Set(h.signals).size).toBe(1);
            expect((h.signals[0] as AbortSignal).aborted).toBe(true);
        } finally { h.close(); }
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
