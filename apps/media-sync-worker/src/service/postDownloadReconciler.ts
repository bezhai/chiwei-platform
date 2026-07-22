import { randomUUID } from 'node:crypto';
import type { ObjectId } from 'mongodb';
import type { PostDownloadReconcileConfig } from '../config/postDownloadReconcile';
import { runPostDownloadSync } from './postDownloadSync';

export interface PostDownloadSourceImage {
  sourceId: ObjectId;
  pixivAddr?: unknown;
  tosFileName?: unknown;
}

export interface PostDownloadCursorLease {
  cursor: ObjectId | null;
  epoch: number;
  leaseToken: string;
}

export interface PostDownloadRetryItem {
  sourceId: ObjectId;
  pixivAddr: string;
  attempts: number;
  leaseToken?: string;
}

export interface PostDownloadReconcileRepository {
  claimCursor(params: {
    leaseToken: string;
    now: Date;
    leaseExpiresAt: Date;
  }): Promise<PostDownloadCursorLease | null>;
  findSourceImagesAfter(cursor: ObjectId | null, limit: number): Promise<PostDownloadSourceImage[]>;
  advanceCursor(params: {
    leaseToken: string;
    sourceId: ObjectId;
    now: Date;
    leaseExpiresAt: Date;
  }): Promise<boolean>;
  completeEpoch(params: {
    leaseToken: string;
    now: Date;
    nextScanAt: Date;
  }): Promise<boolean>;
  releaseCursor(params: { leaseToken: string; now: Date }): Promise<void>;
  recordFailure(params: {
    sourceId: ObjectId;
    pixivAddr: string;
    error: string;
    now: Date;
    nextAttemptAt: Date;
  }): Promise<void>;
  findDueRetries(params: { now: Date; limit: number }): Promise<PostDownloadRetryItem[]>;
  claimRetry(params: {
    sourceId: ObjectId;
    leaseToken: string;
    now: Date;
    leaseExpiresAt: Date;
  }): Promise<PostDownloadRetryItem | null>;
  completeRetry(params: {
    sourceId: ObjectId;
    leaseToken: string;
    now: Date;
  }): Promise<void>;
  deferRetry(params: {
    sourceId: ObjectId;
    leaseToken: string;
    attempts: number;
    error: string;
    now: Date;
    nextAttemptAt: Date;
  }): Promise<void>;
}

export interface PostDownloadReconcileDeps {
  repository: PostDownloadReconcileRepository;
  config: Omit<PostDownloadReconcileConfig, 'idleDelayMs'>;
  syncImage?: (pixivAddr: string) => Promise<unknown>;
  now?: () => Date;
  leaseToken?: () => string;
  sleep?: (ms: number) => Promise<void>;
}

export interface PostDownloadReconcileCycleResult {
  scanned: number;
  retried: number;
  epochCompleted: boolean;
}

export interface PostDownloadReconcileWorker {
  stop(): Promise<void>;
}

export async function processPostDownloadReconcileCycle(
  deps: PostDownloadReconcileDeps
): Promise<PostDownloadReconcileCycleResult> {
  const now = deps.now ?? (() => new Date());
  const nextToken = deps.leaseToken ?? randomUUID;
  const syncImage = deps.syncImage ?? runPostDownloadSync;
  const retried = await processDueRetries(deps, syncImage, now, nextToken);
  const leaseToken = nextToken();
  const claimedAt = now();
  const lease = await deps.repository.claimCursor({
    leaseToken,
    now: claimedAt,
    leaseExpiresAt: after(claimedAt, deps.config.leaseMs),
  });
  if (!lease) return { scanned: 0, retried, epochCompleted: false };

  try {
    const images = await deps.repository.findSourceImagesAfter(lease.cursor, deps.config.batchSize);
    if (images.length === 0) {
      const completedAt = now();
      const completed = await deps.repository.completeEpoch({
        leaseToken,
        now: completedAt,
        nextScanAt: after(completedAt, deps.config.epochDelayMs),
      });
      if (!completed) throw new Error('post-download reconciler lost cursor lease while completing epoch');
      return { scanned: 0, retried, epochCompleted: true };
    }

    for (const image of images) {
      const pixivAddr = eligiblePixivAddr(image);
      if (pixivAddr) {
        try {
          await syncImage(pixivAddr);
        } catch (err) {
          const failedAt = now();
          console.warn(
            `Post-download historical replay failed: pixiv_addr=${pixivAddr} error=${formatError(err)}`
          );
          await deps.repository.recordFailure({
            sourceId: image.sourceId,
            pixivAddr,
            error: formatError(err),
            now: failedAt,
            nextAttemptAt: after(failedAt, deps.config.retryDelayMs),
          });
        }
      }

      const advancedAt = now();
      const advanced = await deps.repository.advanceCursor({
        leaseToken,
        sourceId: image.sourceId,
        now: advancedAt,
        leaseExpiresAt: after(advancedAt, deps.config.leaseMs),
      });
      if (!advanced) throw new Error('post-download reconciler lost cursor lease while advancing cursor');
    }

    return { scanned: images.length, retried, epochCompleted: false };
  } finally {
    await deps.repository.releaseCursor({ leaseToken, now: now() });
  }
}

async function processDueRetries(
  deps: PostDownloadReconcileDeps,
  syncImage: (pixivAddr: string) => Promise<unknown>,
  now: () => Date,
  nextToken: () => string
): Promise<number> {
  const retries = await deps.repository.findDueRetries({
    now: now(),
    limit: deps.config.retryBatchSize,
  });
  let processed = 0;
  for (const retry of retries) {
    const leaseToken = nextToken();
    const claimedAt = now();
    const claimed = await deps.repository.claimRetry({
      sourceId: retry.sourceId,
      leaseToken,
      now: claimedAt,
      leaseExpiresAt: after(claimedAt, deps.config.leaseMs),
    });
    if (!claimed) continue;
    processed += 1;
    try {
      await syncImage(claimed.pixivAddr);
      await deps.repository.completeRetry({
        sourceId: claimed.sourceId,
        leaseToken,
        now: now(),
      });
    } catch (err) {
      const failedAt = now();
      await deps.repository.deferRetry({
        sourceId: claimed.sourceId,
        leaseToken,
        attempts: claimed.attempts + 1,
        error: formatError(err),
        now: failedAt,
        nextAttemptAt: after(failedAt, deps.config.retryDelayMs),
      });
    }
  }
  return processed;
}

export function startPostDownloadReconcileWorker(
  deps: PostDownloadReconcileDeps & { config: PostDownloadReconcileConfig }
): PostDownloadReconcileWorker {
  let stopped = false;
  const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
  const running = (async () => {
    while (!stopped) {
      try {
        await processPostDownloadReconcileCycle(deps);
        // Historical scans are deliberately paced even while work remains so an
        // explicitly enabled backfill cannot monopolize source/local Mongo or Tagger.
        await sleep(deps.config.idleDelayMs);
      } catch (err) {
        console.error('Post-download reconciler cycle failed:', err);
        await sleep(deps.config.idleDelayMs);
      }
    }
  })();

  return {
    async stop() {
      stopped = true;
      await running;
    },
  };
}

function eligiblePixivAddr(image: PostDownloadSourceImage): string | null {
  if (typeof image.pixivAddr !== 'string' || image.pixivAddr.trim() === '') return null;
  if (typeof image.tosFileName !== 'string' || image.tosFileName.trim() === '') return null;
  return image.pixivAddr;
}

function after(date: Date, delayMs: number): Date {
  return new Date(date.getTime() + delayMs);
}

function formatError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
