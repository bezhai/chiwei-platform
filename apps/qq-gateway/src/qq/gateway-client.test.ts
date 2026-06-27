import { describe, it, expect } from 'bun:test';
import type { CustomInboundMessage } from '@inner/shared/protocols';
import { QQGatewayClient, type GatewayWebSocket, type QQGatewayClientDeps } from './gateway-client';

const NOOP_LOG = { info: () => {}, warn: () => {}, error: () => {} };

/** In-memory WebSocket double. close() fires onclose like a real socket. */
class FakeWS implements GatewayWebSocket {
    sent: string[] = [];
    closed = false;
    onopen: ((ev?: unknown) => void) | null = null;
    onmessage: ((ev: { data: unknown }) => void | Promise<void>) | null = null;
    onerror: ((ev?: unknown) => void) | null = null;
    onclose: ((ev?: unknown) => void) | null = null;

    send(data: string): void {
        this.sent.push(data);
    }
    close(): void {
        if (this.closed) return;
        this.closed = true;
        this.onclose?.();
    }

    // ── test helpers ──
    async receive(obj: unknown): Promise<void> {
        await this.onmessage?.({ data: JSON.stringify(obj) });
    }
    lastSent(): Record<string, unknown> {
        return JSON.parse(this.sent[this.sent.length - 1]!);
    }
}

interface CapturedTimer {
    cb: () => unknown;
    ms: number;
}

function makeClient(overrides: Partial<QQGatewayClientDeps> = {}) {
    const created: FakeWS[] = [];
    const forwarded: CustomInboundMessage[] = [];
    const intervals: CapturedTimer[] = [];
    const timeouts: CapturedTimer[] = [];

    const deps: QQGatewayClientDeps = {
        botName: 'chiwei',
        getAccessToken: async () => 'TOK',
        getGatewayUrl: async () => 'wss://gw',
        wsFactory: () => {
            const ws = new FakeWS();
            created.push(ws);
            return ws;
        },
        forwardInbound: async (m) => {
            forwarded.push(m);
        },
        log: NOOP_LOG,
        setIntervalImpl: (cb, ms) => {
            intervals.push({ cb, ms });
            return intervals.length as unknown as ReturnType<typeof setInterval>;
        },
        clearIntervalImpl: () => {},
        setTimeoutImpl: (cb, ms) => {
            timeouts.push({ cb, ms });
            return timeouts.length as unknown as ReturnType<typeof setTimeout>;
        },
        clearTimeoutImpl: () => {},
        ...overrides,
    };
    const client = new QQGatewayClient(deps);
    return { client, created, forwarded, intervals, timeouts };
}

describe('QQGatewayClient: Hello → Identify', () => {
    it('sends an identify (op:2) with intents=33554432 and QQBot-prefixed token after Hello', async () => {
        const { client, created } = makeClient();
        await client.connect();
        const ws = created[0]!;
        await ws.receive({ op: 10, d: { heartbeat_interval: 30000 } });

        const identify = JSON.parse(ws.sent[0]!);
        expect(identify.op).toBe(2);
        expect(identify.d.token).toBe('QQBot TOK');
        expect(identify.d.intents).toBe(33554432);
        expect(identify.d.shard).toEqual([0, 1]);
    });
});

describe('QQGatewayClient: heartbeat', () => {
    it('starts a heartbeat at heartbeat_interval and sends op:1 with d=lastSeq', async () => {
        const { client, created, intervals } = makeClient();
        await client.connect();
        const ws = created[0]!;
        await ws.receive({ op: 10, d: { heartbeat_interval: 12345 } });

        expect(intervals).toHaveLength(1);
        expect(intervals[0]!.ms).toBe(12345);

        // before any seq → d:null
        ws.sent.length = 0;
        intervals[0]!.cb();
        expect(ws.lastSent()).toEqual({ op: 1, d: null });

        // a dispatch carrying `s` bumps lastSeq, reflected in the next heartbeat
        await ws.receive({ op: 0, s: 7, t: 'C2C_MESSAGE_CREATE', d: { author: { user_openid: 'u' }, content: 'x', id: 'm' } });
        ws.sent.length = 0;
        intervals[0]!.cb();
        expect(ws.lastSent()).toEqual({ op: 1, d: 7 });
    });
});

describe('QQGatewayClient: dispatch (op:0)', () => {
    it('normalizes and forwards a non-READY dispatch event', async () => {
        const { client, created, forwarded } = makeClient();
        await client.connect();
        const ws = created[0]!;
        await ws.receive({ op: 10, d: { heartbeat_interval: 30000 } });
        await ws.receive({
            op: 0,
            s: 1,
            t: 'C2C_MESSAGE_CREATE',
            d: { author: { user_openid: 'u1' }, content: 'hi', id: 'M1' },
        });

        expect(forwarded).toHaveLength(1);
        expect(forwarded[0]).toMatchObject({
            chatType: 'direct',
            senderId: 'u1',
            text: 'hi',
            messageId: 'M1',
            botName: 'chiwei',
        });
    });

    it('stores session on READY and ignores RESUMED — neither is forwarded', async () => {
        const { client, created, forwarded } = makeClient();
        await client.connect();
        const ws = created[0]!;
        await ws.receive({ op: 0, s: 1, t: 'READY', d: { session_id: 'sess-1' } });
        await ws.receive({ op: 0, s: 2, t: 'RESUMED', d: {} });
        expect(forwarded).toHaveLength(0);
    });

    it('does not forward unsupported dispatch events (normalize → null)', async () => {
        const { client, created, forwarded } = makeClient();
        await client.connect();
        const ws = created[0]!;
        await ws.receive({ op: 0, s: 1, t: 'GROUP_ADD_ROBOT', d: { group_openid: 'g' } });
        expect(forwarded).toHaveLength(0);
    });
});

describe('QQGatewayClient: reconnect', () => {
    it('reconnects on op:7 Reconnect', async () => {
        const { client, created, timeouts } = makeClient();
        await client.connect();
        expect(created).toHaveLength(1);

        await created[0]!.receive({ op: 7 });
        expect(created[0]!.closed).toBe(true);
        expect(timeouts).toHaveLength(1);

        await timeouts[0]!.cb(); // fire reconnect timer
        expect(created).toHaveLength(2);
    });

    it('reconnects on op:9 Invalid Session and re-identifies fresh', async () => {
        const { client, created, timeouts } = makeClient();
        await client.connect();

        await created[0]!.receive({ op: 9, d: false });
        expect(timeouts).toHaveLength(1);

        await timeouts[0]!.cb();
        expect(created).toHaveLength(2);

        // a fresh identify is sent on the new connection's Hello
        await created[1]!.receive({ op: 10, d: { heartbeat_interval: 30000 } });
        expect(JSON.parse(created[1]!.sent[0]!).op).toBe(2);
    });

    it('reconnects on an unexpected socket close', async () => {
        const { client, created, timeouts } = makeClient();
        await client.connect();
        created[0]!.onclose?.(); // simulate network drop
        expect(timeouts).toHaveLength(1);
        await timeouts[0]!.cb();
        expect(created).toHaveLength(2);
    });

    it('uses an increasing backoff across consecutive reconnects', async () => {
        const { client, created, timeouts } = makeClient();
        await client.connect();

        await created[0]!.receive({ op: 7 });
        expect(timeouts[0]!.ms).toBe(1000);
        await timeouts[0]!.cb();

        await created[1]!.receive({ op: 7 });
        expect(timeouts[1]!.ms).toBe(2000);
    });

    it('stops reconnecting after stop()', async () => {
        const { client, created, timeouts } = makeClient();
        await client.connect();
        client.stop();
        created[0]!.onclose?.();
        expect(timeouts).toHaveLength(0);
    });
});
