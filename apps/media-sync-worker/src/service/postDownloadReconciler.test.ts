import { describe, expect, test } from 'bun:test';
import { ObjectId } from 'mongodb';
import {
  processPostDownloadReconcileCycle,
  type PostDownloadCursorLease,
  type PostDownloadReconcileRepository,
  type PostDownloadRetryItem,
  type PostDownloadSourceImage,
} from './postDownloadReconciler';

const IDS = [1, 2, 3].map((value) => new ObjectId(value.toString(16).padStart(24, '0')));
const NOW = new Date('2026-07-22T00:00:00.000Z');

class FakeRepository implements PostDownloadReconcileRepository {
  cursor: ObjectId | null = null;
  epoch = 0;
  sourceImages: PostDownloadSourceImage[] = [];
  retries: PostDownloadRetryItem[] = [];
  events: string[] = [];
  allowAdvance = true;

  async claimCursor(params: { leaseToken: string }): Promise<PostDownloadCursorLease | null> {
    this.events.push(`claim-cursor:${params.leaseToken}`);
    return { cursor: this.cursor, epoch: this.epoch, leaseToken: params.leaseToken };
  }

  async findSourceImagesAfter(cursor: ObjectId | null, limit: number) {
    return this.sourceImages
      .filter((item) => cursor === null || item.sourceId.toHexString() > cursor.toHexString())
      .slice(0, limit);
  }

  async advanceCursor(params: { sourceId: ObjectId }) {
    this.events.push(`advance:${params.sourceId.toHexString()}`);
    if (!this.allowAdvance) return false;
    this.cursor = params.sourceId;
    return true;
  }

  async completeEpoch() {
    this.events.push('complete-epoch');
    this.cursor = null;
    this.epoch += 1;
    return true;
  }

  async releaseCursor() {
    this.events.push('release-cursor');
  }

  async recordFailure(params: { sourceId: ObjectId; pixivAddr: string; error: string }) {
    this.events.push(`record-failure:${params.pixivAddr}`);
    this.retries.push({
      sourceId: params.sourceId,
      pixivAddr: params.pixivAddr,
      attempts: 1,
    });
  }

  async findDueRetries(params: { limit: number }) {
    return this.retries.slice(0, params.limit);
  }

  async claimRetry(params: { sourceId: ObjectId; leaseToken: string }) {
    const retry = this.retries.find((item) => item.sourceId.equals(params.sourceId));
    return retry ? { ...retry, leaseToken: params.leaseToken } : null;
  }

  async completeRetry(params: { sourceId: ObjectId }) {
    this.events.push(`complete-retry:${params.sourceId.toHexString()}`);
    this.retries = this.retries.filter((item) => !item.sourceId.equals(params.sourceId));
  }

  async deferRetry(params: { sourceId: ObjectId; error: string }) {
    this.events.push(`defer-retry:${params.sourceId.toHexString()}:${params.error}`);
  }
}

const config = {
  batchSize: 10,
  retryBatchSize: 10,
  retryDelayMs: 60_000,
  leaseMs: 30_000,
  epochDelayMs: 300_000,
};

describe('processPostDownloadReconcileCycle', () => {
  test('scans the whole source ordering but only replays eligible images', async () => {
    const repository = new FakeRepository();
    repository.sourceImages = [
      { sourceId: IDS[0], pixivAddr: 'missing-object.jpg' },
      { sourceId: IDS[1], pixivAddr: 'ready.jpg', tosFileName: 'pixiv/ready.jpg' },
      { sourceId: IDS[2], pixivAddr: '   ', tosFileName: 'pixiv/invalid.jpg' },
    ];
    const synced: string[] = [];

    const result = await processPostDownloadReconcileCycle({
      repository,
      config,
      now: () => NOW,
      leaseToken: () => 'cursor-token',
      syncImage: async (pixivAddr) => { synced.push(pixivAddr); },
    });

    expect(result).toEqual({ scanned: 3, retried: 0, epochCompleted: false });
    expect(synced).toEqual(['ready.jpg']);
    expect(repository.cursor).toEqual(IDS[2]);
  });

  test('durably records a poison image before advancing past it', async () => {
    const repository = new FakeRepository();
    repository.sourceImages = [
      { sourceId: IDS[0], pixivAddr: 'poison.jpg', tosFileName: 'pixiv/poison.jpg' },
      { sourceId: IDS[1], pixivAddr: 'healthy.jpg', tosFileName: 'pixiv/healthy.jpg' },
    ];

    await processPostDownloadReconcileCycle({
      repository,
      config,
      now: () => NOW,
      leaseToken: () => 'token',
      syncImage: async (pixivAddr) => {
        if (pixivAddr === 'poison.jpg') throw new Error('mirror unavailable');
      },
    });

    const failureIndex = repository.events.indexOf('record-failure:poison.jpg');
    const advanceIndex = repository.events.indexOf(`advance:${IDS[0].toHexString()}`);
    expect(failureIndex).toBeGreaterThanOrEqual(0);
    expect(advanceIndex).toBeGreaterThan(failureIndex);
    expect(repository.cursor).toEqual(IDS[1]);
    expect(repository.retries.map((item) => item.pixivAddr)).toEqual(['poison.jpg']);
  });

  test('stops when a stale cursor owner loses its fencing token', async () => {
    const repository = new FakeRepository();
    repository.allowAdvance = false;
    repository.sourceImages = [
      { sourceId: IDS[0], pixivAddr: 'ready.jpg', tosFileName: 'pixiv/ready.jpg' },
      { sourceId: IDS[1], pixivAddr: 'never-reached.jpg', tosFileName: 'pixiv/never.jpg' },
    ];
    const synced: string[] = [];

    await expect(processPostDownloadReconcileCycle({
      repository,
      config,
      now: () => NOW,
      leaseToken: () => 'stale-token',
      syncImage: async (pixivAddr) => { synced.push(pixivAddr); },
    })).rejects.toThrow('lost cursor lease');

    expect(synced).toEqual(['ready.jpg']);
    expect(repository.cursor).toBeNull();
  });

  test('starts a new epoch so a previously ineligible old document can be replayed', async () => {
    const repository = new FakeRepository();
    repository.sourceImages = [{ sourceId: IDS[0], pixivAddr: 'late.jpg' }];
    const synced: string[] = [];
    const deps = {
      repository,
      config,
      now: () => NOW,
      leaseToken: () => 'token',
      syncImage: async (pixivAddr: string) => { synced.push(pixivAddr); },
    };

    await processPostDownloadReconcileCycle(deps);
    const end = await processPostDownloadReconcileCycle(deps);
    repository.sourceImages[0].tosFileName = 'pixiv/late.jpg';
    await processPostDownloadReconcileCycle(deps);

    expect(end.epochCompleted).toBeTrue();
    expect(repository.epoch).toBe(1);
    expect(synced).toEqual(['late.jpg']);
  });

  test('retries durable failures independently of the main cursor', async () => {
    const repository = new FakeRepository();
    repository.retries = [{ sourceId: IDS[0], pixivAddr: 'retry.jpg', attempts: 2 }];
    const synced: string[] = [];

    const result = await processPostDownloadReconcileCycle({
      repository,
      config,
      now: () => NOW,
      leaseToken: (() => {
        let value = 0;
        return () => `token-${++value}`;
      })(),
      syncImage: async (pixivAddr) => { synced.push(pixivAddr); },
    });

    expect(result.retried).toBe(1);
    expect(synced).toEqual(['retry.jpg']);
    expect(repository.retries).toEqual([]);
  });
});
