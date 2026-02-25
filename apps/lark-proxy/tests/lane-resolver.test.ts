import { describe, it, expect, beforeEach, mock } from 'bun:test';
import { LaneResolver } from '../src/lane-resolver';

describe('LaneResolver', () => {
    let mockPool: { query: ReturnType<typeof mock> };
    let resolver: LaneResolver;

    beforeEach(() => {
        mockPool = {
            query: mock(() => Promise.resolve({ rows: [] })),
        };
        resolver = new LaneResolver(mockPool as any);
    });

    it('should return lane_name when routing exists', async () => {
        mockPool.query.mockImplementation(() =>
            Promise.resolve({ rows: [{ lane_name: 'feat-test' }] }),
        );

        const lane = await resolver.resolve('bot', 'my-bot');
        expect(lane).toBe('feat-test');
        expect(mockPool.query).toHaveBeenCalledWith(
            expect.stringContaining('SELECT lane_name'),
            ['bot', 'my-bot'],
        );
    });

    it('should return null when no routing exists', async () => {
        mockPool.query.mockImplementation(() => Promise.resolve({ rows: [] }));

        const lane = await resolver.resolve('bot', 'unknown-bot');
        expect(lane).toBeNull();
    });

    it('should cache results and not re-query within TTL', async () => {
        mockPool.query.mockImplementation(() =>
            Promise.resolve({ rows: [{ lane_name: 'feat-cached' }] }),
        );

        const lane1 = await resolver.resolve('bot', 'cached-bot');
        const lane2 = await resolver.resolve('bot', 'cached-bot');

        expect(lane1).toBe('feat-cached');
        expect(lane2).toBe('feat-cached');
        expect(mockPool.query).toHaveBeenCalledTimes(1);
    });

    it('should use different cache keys for different route types', async () => {
        let callCount = 0;
        mockPool.query.mockImplementation(() => {
            callCount++;
            if (callCount === 1) return Promise.resolve({ rows: [{ lane_name: 'lane-bot' }] });
            return Promise.resolve({ rows: [{ lane_name: 'lane-chat' }] });
        });

        const botLane = await resolver.resolve('bot', 'key1');
        const chatLane = await resolver.resolve('chat', 'key1');

        expect(botLane).toBe('lane-bot');
        expect(chatLane).toBe('lane-chat');
        expect(mockPool.query).toHaveBeenCalledTimes(2);
    });

    it('should clear cache on clearCache()', async () => {
        mockPool.query.mockImplementation(() =>
            Promise.resolve({ rows: [{ lane_name: 'feat-a' }] }),
        );

        await resolver.resolve('bot', 'bot1');
        expect(mockPool.query).toHaveBeenCalledTimes(1);

        resolver.clearCache();

        mockPool.query.mockImplementation(() =>
            Promise.resolve({ rows: [{ lane_name: 'feat-b' }] }),
        );
        const lane = await resolver.resolve('bot', 'bot1');

        expect(lane).toBe('feat-b');
        expect(mockPool.query).toHaveBeenCalledTimes(2);
    });
});
