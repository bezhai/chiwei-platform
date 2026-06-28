import { describe, it, expect } from 'bun:test';
import { QQClient } from './api';

interface FetchCall {
    url: string;
    init: RequestInit;
}

function jsonResponse(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function makeClient(opts: {
    onFetch: (call: FetchCall) => Response | Promise<Response>;
    now?: () => number;
}) {
    const calls: FetchCall[] = [];
    const fetchImpl = (async (url: string | URL | Request, init?: RequestInit) => {
        const call = { url: String(url), init: init ?? {} };
        calls.push(call);
        return opts.onFetch(call);
    }) as unknown as typeof fetch;

    const client = new QQClient({
        appId: 'app-1',
        clientSecret: 'secret-1',
        fetchImpl,
        now: opts.now,
    });
    return { client, calls };
}

describe('QQClient.getAccessToken', () => {
    it('fetches once and caches the token', async () => {
        const { client, calls } = makeClient({
            onFetch: () => jsonResponse({ access_token: 'tok-abc', expires_in: 7200 }),
        });
        expect(await client.getAccessToken()).toBe('tok-abc');
        expect(await client.getAccessToken()).toBe('tok-abc');
        const tokenCalls = calls.filter((c) => c.url.includes('getAppAccessToken'));
        expect(tokenCalls).toHaveLength(1);
        // posts appId + clientSecret as JSON
        expect(JSON.parse(tokenCalls[0].init.body as string)).toEqual({ appId: 'app-1', clientSecret: 'secret-1' });
    });

    it('singleflights concurrent refreshes into one fetch', async () => {
        let resolved = 0;
        const { client, calls } = makeClient({
            onFetch: async () => {
                await new Promise((r) => setTimeout(r, 5));
                resolved++;
                return jsonResponse({ access_token: `tok-${resolved}`, expires_in: 7200 });
            },
        });
        const [a, b] = await Promise.all([client.getAccessToken(), client.getAccessToken()]);
        expect(a).toBe(b);
        expect(calls.filter((c) => c.url.includes('getAppAccessToken'))).toHaveLength(1);
    });

    it('refreshes after the token expires', async () => {
        let t = 1_000_000;
        let n = 0;
        const { client, calls } = makeClient({
            now: () => t,
            onFetch: () => {
                n++;
                return jsonResponse({ access_token: `tok-${n}`, expires_in: 100 }); // 100s ttl
            },
        });
        expect(await client.getAccessToken()).toBe('tok-1');
        t += 200 * 1000; // advance well past expiry
        expect(await client.getAccessToken()).toBe('tok-2');
        expect(calls.filter((c) => c.url.includes('getAppAccessToken'))).toHaveLength(2);
    });

    it('throws when the token endpoint returns no access_token', async () => {
        const { client } = makeClient({ onFetch: () => jsonResponse({ message: 'bad app' }, 400) });
        await expect(client.getAccessToken()).rejects.toThrow();
    });
});

describe('QQClient.getGatewayUrl', () => {
    const tokenOk = (call: FetchCall): Response | null =>
        call.url.includes('getAppAccessToken') ? jsonResponse({ access_token: 'TOK', expires_in: 7200 }) : null;

    it('GETs /gateway with QQBot auth and returns the wss url', async () => {
        const { client, calls } = makeClient({
            onFetch: (call) => tokenOk(call) ?? jsonResponse({ url: 'wss://api.sgroup.qq.com/websocket' }),
        });
        const url = await client.getGatewayUrl();
        expect(url).toBe('wss://api.sgroup.qq.com/websocket');

        const gw = calls.find((c) => c.url.endsWith('/gateway'))!;
        expect(gw.url).toBe('https://api.sgroup.qq.com/gateway');
        expect(gw.init.method).toBe('GET');
        expect((gw.init.headers as Record<string, string>)['Authorization']).toBe('QQBot TOK');
    });

    it('throws when the gateway endpoint returns no url', async () => {
        const { client } = makeClient({ onFetch: (call) => tokenOk(call) ?? jsonResponse({}, 200) });
        await expect(client.getGatewayUrl()).rejects.toThrow();
    });

    it('throws on a non-2xx gateway response', async () => {
        const { client } = makeClient({ onFetch: (call) => tokenOk(call) ?? jsonResponse({ message: 'no' }, 500) });
        await expect(client.getGatewayUrl()).rejects.toThrow();
    });
});

describe('QQClient passive send', () => {
    const tokenOk = (call: FetchCall): Response | null =>
        call.url.includes('getAppAccessToken') ? jsonResponse({ access_token: 'TOK', expires_in: 7200 }) : null;

    it('sendC2CMessage posts to /v2/users/{openid}/messages with msg_id + msg_seq + QQBot auth', async () => {
        const { client, calls } = makeClient({
            onFetch: (call) => tokenOk(call) ?? jsonResponse({ id: 'resp-1' }),
        });
        const res = await client.sendC2CMessage('user-9', '你好', { msgId: 'MID', msgSeq: 3 });
        expect(res.id).toBe('resp-1');

        const send = calls.find((c) => c.url.includes('/v2/users/'))!;
        expect(send.url).toBe('https://api.sgroup.qq.com/v2/users/user-9/messages');
        expect(send.init.method).toBe('POST');
        expect((send.init.headers as Record<string, string>)['Authorization']).toBe('QQBot TOK');
        expect(JSON.parse(send.init.body as string)).toEqual({
            content: '你好',
            msg_type: 0,
            msg_id: 'MID',
            msg_seq: 3,
        });
    });

    it('sendGroupMessage posts to /v2/groups/{group}/messages with msg_id + msg_seq', async () => {
        const { client, calls } = makeClient({
            onFetch: (call) => tokenOk(call) ?? jsonResponse({ id: 'resp-2' }),
        });
        await client.sendGroupMessage('group-7', 'hi', { msgId: 'GID', msgSeq: 1 });

        const send = calls.find((c) => c.url.includes('/v2/groups/'))!;
        expect(send.url).toBe('https://api.sgroup.qq.com/v2/groups/group-7/messages');
        expect(JSON.parse(send.init.body as string)).toEqual({
            content: 'hi',
            msg_type: 0,
            msg_id: 'GID',
            msg_seq: 1,
        });
    });

    it('throws on a non-2xx send response', async () => {
        const { client } = makeClient({
            onFetch: (call) => tokenOk(call) ?? jsonResponse({ message: 'nope', code: 1234 }, 400),
        });
        await expect(client.sendC2CMessage('u', 'x', { msgId: 'M', msgSeq: 1 })).rejects.toThrow();
    });
});
