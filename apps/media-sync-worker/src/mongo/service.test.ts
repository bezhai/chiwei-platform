import { describe, expect, it } from 'bun:test';
import {
    buildClaimableTaskFilter,
    buildClaimUpdate,
    buildCompletionFilter,
    buildExhaustedReclaimFilter,
    buildDeadLetterUpdate,
    buildImageByPixivAddrFilter,
} from './service';
import { DownloadTask, DownloadTaskStatus } from './types';

// buildImageByPixivAddrFilter is a pure query builder so it can be unit-tested
// without importing ./client (which would trigger a real Mongo connect) and
// without mock.module (which pollutes sibling tests in the same bun run).
describe('buildImageByPixivAddrFilter', () => {
    it('empty pixivAddr -> null (caller short-circuits to null doc)', () => {
        expect(buildImageByPixivAddrFilter('')).toBeNull();
    });

    it('non-empty pixivAddr -> matches pixiv_addr AND requires a non-empty tos_file_name', () => {
        const filter = buildImageByPixivAddrFilter('123_p0.png');

        expect(filter).not.toBeNull();
        expect(filter!.pixiv_addr).toBe('123_p0.png');

        // a doc whose tos_file_name is missing or empty must NOT match this filter,
        // so the $nin guard against null/'' must be present
        expect(filter!.tos_file_name).toEqual({ $nin: [null, ''] } as any);
    });
});

describe('buildClaimableTaskFilter', () => {
    const now = new Date('2026-07-06T12:00:00.000Z');
    const reclaimMs = 90 * 60 * 1000;

    it('claims Pending and Fail unconditionally', () => {
        const filter = buildClaimableTaskFilter(now, reclaimMs);

        expect(filter.$or?.[0]).toEqual({
            status: { $in: [DownloadTaskStatus.Pending, DownloadTaskStatus.Fail] },
        });
    });

    it('claims Running only when last_run_time is older than the reclaim threshold', () => {
        const filter = buildClaimableTaskFilter(now, reclaimMs);

        expect(filter.$or?.[1]).toEqual({
            status: DownloadTaskStatus.Running,
            last_run_time: { $lt: new Date('2026-07-06T10:30:00.000Z') },
        });
    });

    it('has no other branch, so fresh Running / Success / Dead can never match', () => {
        const filter = buildClaimableTaskFilter(now, reclaimMs);

        expect(Object.keys(filter)).toEqual(['$or']);
        expect(filter.$or).toHaveLength(2);
    });
});

describe('buildClaimUpdate', () => {
    it('marks Running, stamps a new generation (last_run_time) and consumes retry budget atomically', () => {
        const now = new Date('2026-07-06T12:00:00.000Z');

        expect(buildClaimUpdate(now)).toEqual({
            $set: {
                status: DownloadTaskStatus.Running,
                last_run_time: now,
                update_time: now,
                last_run_error: '',
            },
            $inc: { retry_time: 1 },
        });
    });
});

// A poison task (one that hangs the consumer every time) never reaches the
// fail() completion path, so retry_time keeps growing via claim $inc while the
// task bounces between Running and reclaim forever. The dead-letter sweep must
// catch exactly these: reclaim-eligible Running docs whose retry budget is gone.
describe('buildExhaustedReclaimFilter', () => {
    const now = new Date('2026-07-06T12:00:00.000Z');
    const reclaimMs = 90 * 60 * 1000;

    it('matches only stale Running docs whose retry budget is exhausted', () => {
        expect(buildExhaustedReclaimFilter(now, reclaimMs)).toEqual({
            status: DownloadTaskStatus.Running,
            last_run_time: { $lt: new Date('2026-07-06T10:30:00.000Z') },
            retry_time: { $gte: DownloadTask.MaxRetryTime },
        } as any);
    });

    it('never matches docs that still have retry budget (they go back through reclaim)', () => {
        const filter = buildExhaustedReclaimFilter(now, reclaimMs) as any;

        expect(filter.retry_time.$gte).toBe(3);
    });
});

describe('buildDeadLetterUpdate', () => {
    // returns bare fields (NOT an {$set:...} document): MongoCollection.updateMany
    // wraps its update argument in $set itself, so a pre-wrapped document would
    // become {$set:{$set:...}} and fail at runtime
    it('marks Dead with an explanation so operators can tell sweep from download failure', () => {
        const now = new Date('2026-07-06T12:00:00.000Z');
        const update = buildDeadLetterUpdate(now) as any;

        expect(update.$set).toBeUndefined();
        expect(update.status).toBe(DownloadTaskStatus.Dead);
        expect(update.update_time).toEqual(now);
        expect(update.last_run_error).toContain('reclaim');
    });
});

describe('buildCompletionFilter', () => {
    it('pins illust_id AND the claimed generation, so a doc re-claimed later (new last_run_time) does not match', () => {
        const claimedRunTime = new Date('2026-07-06T12:00:00.000Z');

        expect(buildCompletionFilter('123', claimedRunTime)).toEqual({
            illust_id: '123',
            last_run_time: claimedRunTime,
        } as any);
    });

    it('undefined generation only matches docs with null/missing last_run_time (never a claimed doc)', () => {
        expect(buildCompletionFilter('123', undefined)).toEqual({
            illust_id: '123',
            last_run_time: null,
        } as any);
    });
});
