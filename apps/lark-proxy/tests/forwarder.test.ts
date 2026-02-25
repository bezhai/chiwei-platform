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
    });

    it('should forward to prod namespace when no lane binding', async () => {
        mockResolver.resolve.mockImplementation(() => Promise.resolve(null));

        await (forwarder as any).doForward('im.message.receive_v1', 'my-bot', { test: 1 });

        expect(mockFetch).toHaveBeenCalledWith(
            'http://main-server.prod.svc.cluster.local:3000/api/internal/lark-event',
            expect.objectContaining({
                method: 'POST',
                headers: expect.objectContaining({
                    'X-App-Name': 'my-bot',
                    Authorization: 'Bearer test-secret',
                }),
            }),
        );

        const body = JSON.parse(mockFetch.mock.calls[0][1].body);
        expect(body.event_type).toBe('im.message.receive_v1');
        expect(body.params).toEqual({ test: 1 });
    });

    it('should forward to lane namespace when binding exists', async () => {
        mockResolver.resolve.mockImplementation(() => Promise.resolve('feat-xxx'));

        await (forwarder as any).doForward('im.message.receive_v1', 'dev-bot', {});

        expect(mockFetch).toHaveBeenCalledWith(
            'http://main-server.lane-feat-xxx.svc.cluster.local:3000/api/internal/lark-event',
            expect.anything(),
        );
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

    it('should log error when main-server responds with non-ok status', async () => {
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
            expect.stringContaining('main-server responded 500'),
        );
        consoleSpy.mockRestore();
    });
});
