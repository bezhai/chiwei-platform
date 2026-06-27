import { describe, it, expect } from 'bun:test';
import type { CustomInboundMessage } from '@inner/shared/protocols';
import { createInboundForwarder, type InboundFetcher } from './inbound-forwarder';

const NOOP_LOG = { info: () => {}, warn: () => {}, error: () => {} };

function validMsg(): CustomInboundMessage {
    return {
        botName: 'chiwei',
        chatType: 'direct',
        conversationId: 'u1',
        senderId: 'u1',
        text: 'hi',
        messageId: 'M1',
        timestamp: '2026-06-27T00:00:00Z',
    };
}

describe('createInboundForwarder', () => {
    it('POSTs the message to channel-server with Bearer auth and JSON body', async () => {
        const calls: Array<{ service: string; path: string; init: RequestInit }> = [];
        const fetcher: InboundFetcher = {
            fetch: async (service, path, init) => {
                calls.push({ service, path, init: init ?? {} });
                return new Response('{"ok":true}', { status: 200 });
            },
        };
        const forward = createInboundForwarder({
            fetcher,
            service: 'channel-server',
            path: '/api/internal/qq/inbound',
            innerSecret: 'inner-1',
            log: NOOP_LOG,
        });

        await forward(validMsg());
        expect(calls).toHaveLength(1);
        expect(calls[0].service).toBe('channel-server');
        expect(calls[0].path).toBe('/api/internal/qq/inbound');
        expect(calls[0].init.method).toBe('POST');
        const headers = calls[0].init.headers as Record<string, string>;
        expect(headers['Authorization']).toBe('Bearer inner-1');
        expect(headers['Content-Type']).toBe('application/json');
        expect(JSON.parse(calls[0].init.body as string)).toMatchObject({ senderId: 'u1', messageId: 'M1' });
    });

    it('validates before sending and throws on an invalid message (never hits the wire)', async () => {
        let hit = false;
        const fetcher: InboundFetcher = {
            fetch: async () => {
                hit = true;
                return new Response('{}', { status: 200 });
            },
        };
        const forward = createInboundForwarder({
            fetcher,
            service: 'channel-server',
            path: '/api/internal/qq/inbound',
            innerSecret: 'inner-1',
            log: NOOP_LOG,
        });

        const bad = { ...validMsg(), senderId: 123 } as unknown as CustomInboundMessage;
        await expect(forward(bad)).rejects.toThrow(/senderId/);
        expect(hit).toBe(false);
    });

    it('throws when channel-server responds non-2xx', async () => {
        const fetcher: InboundFetcher = {
            fetch: async () => new Response('nope', { status: 500 }),
        };
        const forward = createInboundForwarder({
            fetcher,
            service: 'channel-server',
            path: '/api/internal/qq/inbound',
            innerSecret: 'inner-1',
            log: NOOP_LOG,
        });
        await expect(forward(validMsg())).rejects.toThrow(/500/);
    });
});
