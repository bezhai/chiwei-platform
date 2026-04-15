import { describe, it, expect, beforeEach, afterEach, mock, spyOn } from 'bun:test';
import { EventForwarder } from '../src/forwarder';
import { LaneResolver } from '../src/lane-resolver';

// Mock global fetch
const mockFetch = mock(() =>
    Promise.resolve({ ok: true, text: () => Promise.resolve('') } as Response),
);
globalThis.fetch = mockFetch as any;

describe('EventForwarder', () => {
    let mockResolver: { resolve: ReturnType<typeof mock>; clearCache: ReturnType<typeof mock> };
    let forwarder: EventForwarder;

    beforeEach(() => {
        process.env.INNER_HTTP_SECRET = 'test-secret';
        process.env.LARK_SERVER_URL = 'http://lark-server:3000';
        mockResolver = {
            resolve: mock(() => Promise.resolve(null)),
            clearCache: mock(() => {}),
        };
        forwarder = new EventForwarder(mockResolver as unknown as LaneResolver);
        mockFetch.mockReset();
        mockFetch.mockImplementation(() =>
            Promise.resolve({ ok: true, text: () => Promise.resolve('') } as Response),
        );
    });

    afterEach(() => {
        delete process.env.INNER_HTTP_SECRET;
        delete process.env.LARK_SERVER_URL;
    });

    it('should forward to lark-server prod when no lane binding', async () => {
        mockResolver.resolve.mockImplementation(() => Promise.resolve(null));

        await (forwarder as any).doForward('im.message.receive_v1', 'my-bot', { test: 1 });

        expect(mockFetch).toHaveBeenCalledWith(
            'http://lark-server:3000/api/internal/lark-event',
            expect.objectContaining({
                method: 'POST',
                headers: expect.objectContaining({
                    'X-App-Name': 'my-bot',
                    Authorization: 'Bearer test-secret',
                }),
            }),
        );

        // x-ctx-lane should NOT be present when no lane
        const headers = mockFetch.mock.calls[0][1].headers;
        expect(headers['x-ctx-lane']).toBeUndefined();

        const body = JSON.parse(mockFetch.mock.calls[0][1].body);
        expect(body.event_type).toBe('im.message.receive_v1');
        expect(body.params).toEqual({ test: 1 });
    });

    it('should forward to same URL with x-ctx-lane when lane binding exists (sidecar routes)', async () => {
        mockResolver.resolve.mockImplementation(() => Promise.resolve('feat-xxx'));

        await (forwarder as any).doForward('im.message.receive_v1', 'dev-bot', {});

        expect(mockFetch).toHaveBeenCalledWith(
            'http://lark-server:3000/api/internal/lark-event',
            expect.objectContaining({
                headers: expect.objectContaining({
                    'x-ctx-lane': 'feat-xxx',
                }),
            }),
        );
    });

    it('should forward to prod URL without x-ctx-lane when lane is prod', async () => {
        mockResolver.resolve.mockImplementation(() => Promise.resolve('prod'));

        await (forwarder as any).doForward('im.message.receive_v1', 'dev-bot', {});

        expect(mockFetch).toHaveBeenCalledWith(
            'http://lark-server:3000/api/internal/lark-event',
            expect.anything(),
        );
        const headers = mockFetch.mock.calls[0][1].headers;
        expect(headers['x-ctx-lane']).toBeUndefined();
    });

    it('createHandler should return {} immediately', () => {
        mockResolver.resolve.mockImplementation(() => Promise.resolve(null));
        const handler = forwarder.createHandler('my-bot');
        const result = handler({ event_type: 'im.message.receive_v1' });
        expect(result).toEqual({});
    });

    it('createCardHandler should return {} immediately', () => {
        mockResolver.resolve.mockImplementation(() => Promise.resolve(null));
        const handler = forwarder.createCardHandler('my-bot');
        const result = handler({ action: { value: { type: 'test' } } });
        expect(result).toEqual({});
    });

    it('should log error when lark-server responds with non-ok status', async () => {
        mockResolver.resolve.mockImplementation(() => Promise.resolve(null));
        mockFetch.mockImplementation(() =>
            Promise.resolve({
                ok: false,
                status: 500,
                text: () => Promise.resolve('Internal Server Error'),
            } as Response),
        );

        const consoleSpy = spyOn(console, 'error').mockImplementation(() => {});

        await (forwarder as any).doForward('im.message.receive_v1', 'my-bot', {});

        expect(consoleSpy).toHaveBeenCalledWith(
            expect.stringContaining('lark-server responded 500'),
        );
        consoleSpy.mockRestore();
    });

    it('should extract chat_id and set x-ctx-lane (sidecar routes to lane instance)', async () => {
        mockResolver.resolve.mockImplementation((_type: string, key: string) =>
            key === 'oc_test_chat' ? Promise.resolve('feat-chat') : Promise.resolve(null),
        );

        await (forwarder as any).doForward('im.message.receive_v1', 'my-bot', {
            message: { chat_id: 'oc_test_chat' },
        });

        expect(mockResolver.resolve).toHaveBeenCalledWith('chat', 'oc_test_chat');
        expect(mockFetch).toHaveBeenCalledWith(
            'http://lark-server:3000/api/internal/lark-event',
            expect.objectContaining({
                headers: expect.objectContaining({
                    'x-ctx-lane': 'feat-chat',
                }),
            }),
        );
    });
});
