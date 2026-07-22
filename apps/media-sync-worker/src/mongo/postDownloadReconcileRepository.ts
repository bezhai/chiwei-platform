import type { Collection, Document, Filter, ObjectId } from 'mongodb';
import { MongoServerError } from 'mongodb';
import { ImgCollection } from './client';
import { getPixivImageMirrorMongoService } from './imageMirror';
import type {
  PostDownloadCursorLease,
  PostDownloadReconcileRepository,
  PostDownloadRetryItem,
  PostDownloadSourceImage,
} from '../service/postDownloadReconciler';
import type { PixivImageInfo } from './types';

const CURSOR_ID = 'post_download_reconciler';

interface CursorDocument extends Document {
  _id: string;
  cursor: ObjectId | null;
  epoch: number;
  lease_token?: string;
  lease_expires_at?: Date;
  next_scan_at?: Date;
}

interface RetryDocument extends Document {
  source_id: ObjectId;
  pixiv_addr: string;
  status: 'pending' | 'processing' | 'completed';
  attempts: number;
  lease_token?: string;
  lease_expires_at?: Date;
}

export function buildPostDownloadCursorOwnerFilter(leaseToken: string): Filter<CursorDocument> {
  return { _id: CURSOR_ID, lease_token: leaseToken };
}

export function buildPostDownloadRetryOwnerFilter(
  sourceId: ObjectId,
  leaseToken: string
): Filter<RetryDocument> {
  return { source_id: sourceId, status: 'processing', lease_token: leaseToken };
}

export function buildPostDownloadRetryDueFilter(now: Date): Filter<RetryDocument> {
  return {
    $or: [
      {
        $and: [
          { status: 'pending' },
          { next_attempt_at: { $lte: now } },
          {
            $or: [
              { lease_token: { $exists: false } },
              { lease_expires_at: { $lte: now } },
            ],
          },
        ],
      },
      { status: 'processing', lease_expires_at: { $lte: now } },
    ],
  };
}

export function buildPostDownloadRetryWritableFilter(
  sourceId: ObjectId,
  now: Date
): Filter<RetryDocument> {
  return {
    source_id: sourceId,
    $or: [
      { status: { $ne: 'processing' } },
      { lease_expires_at: { $lte: now } },
    ],
  };
}

export class MongoPostDownloadReconcileRepository implements PostDownloadReconcileRepository {
  constructor(
    private readonly source: Collection<PixivImageInfo>,
    private readonly cursor: Collection<CursorDocument>,
    private readonly retries: Collection<RetryDocument>
  ) {}

  async claimCursor(params: {
    leaseToken: string;
    now: Date;
    leaseExpiresAt: Date;
  }): Promise<PostDownloadCursorLease | null> {
    try {
      const doc = await this.cursor.findOneAndUpdate(
        {
          _id: CURSOR_ID,
          $and: [
            { $or: [{ next_scan_at: { $exists: false } }, { next_scan_at: { $lte: params.now } }] },
            { $or: [{ lease_token: { $exists: false } }, { lease_expires_at: { $lte: params.now } }] },
          ],
        },
        {
          $setOnInsert: { cursor: null, epoch: 0 },
          $set: {
            lease_token: params.leaseToken,
            lease_expires_at: params.leaseExpiresAt,
            updated_at: params.now,
          },
        },
        { upsert: true, returnDocument: 'after' }
      );
      if (!doc) return null;
      return {
        cursor: doc.cursor ?? null,
        epoch: doc.epoch ?? 0,
        leaseToken: params.leaseToken,
      };
    } catch (err) {
      if (err instanceof MongoServerError && err.code === 11000) return null;
      throw err;
    }
  }

  async findSourceImagesAfter(cursor: ObjectId | null, limit: number): Promise<PostDownloadSourceImage[]> {
    const filter = cursor ? { _id: { $gt: cursor } } : {};
    const docs = await this.source.find(filter).sort({ _id: 1 }).limit(limit).toArray();
    return docs.map((doc) => ({
      sourceId: doc._id,
      pixivAddr: doc.pixiv_addr,
      tosFileName: doc.tos_file_name,
    }));
  }

  async advanceCursor(params: {
    leaseToken: string;
    sourceId: ObjectId;
    now: Date;
    leaseExpiresAt: Date;
  }): Promise<boolean> {
    const result = await this.cursor.updateOne(
      buildPostDownloadCursorOwnerFilter(params.leaseToken),
      {
        $set: {
          cursor: params.sourceId,
          lease_expires_at: params.leaseExpiresAt,
          updated_at: params.now,
        },
      }
    );
    return result.matchedCount === 1;
  }

  async completeEpoch(params: {
    leaseToken: string;
    now: Date;
    nextScanAt: Date;
  }): Promise<boolean> {
    const result = await this.cursor.updateOne(
      buildPostDownloadCursorOwnerFilter(params.leaseToken),
      {
        $set: { cursor: null, next_scan_at: params.nextScanAt, updated_at: params.now },
        $inc: { epoch: 1 },
        $unset: { lease_token: '', lease_expires_at: '' },
      }
    );
    return result.matchedCount === 1;
  }

  async releaseCursor(params: { leaseToken: string; now: Date }): Promise<void> {
    await this.cursor.updateOne(
      buildPostDownloadCursorOwnerFilter(params.leaseToken),
      { $set: { updated_at: params.now }, $unset: { lease_token: '', lease_expires_at: '' } }
    );
  }

  async recordFailure(params: {
    sourceId: ObjectId;
    pixivAddr: string;
    error: string;
    now: Date;
    nextAttemptAt: Date;
  }): Promise<void> {
    try {
      await this.retries.updateOne(
        buildPostDownloadRetryWritableFilter(params.sourceId, params.now),
        {
          $setOnInsert: { source_id: params.sourceId, created_at: params.now },
          $set: {
            pixiv_addr: params.pixivAddr,
            status: 'pending',
            error: params.error,
            next_attempt_at: params.nextAttemptAt,
            updated_at: params.now,
          },
          $inc: { attempts: 1 },
          $unset: { lease_token: '', lease_expires_at: '' },
        },
        { upsert: true }
      );
    } catch (err) {
      // An active retry row is already the durable representation of this
      // failure. The unique source_id index turns the non-matching upsert into
      // E11000; leave the newer retry owner untouched.
      if (err instanceof MongoServerError && err.code === 11000) return;
      throw err;
    }
  }

  async findDueRetries(params: { now: Date; limit: number }): Promise<PostDownloadRetryItem[]> {
    const docs = await this.retries.find(buildPostDownloadRetryDueFilter(params.now))
      .sort({ next_attempt_at: 1, source_id: 1 }).limit(params.limit).toArray();
    return docs.map(toRetryItem);
  }

  async claimRetry(params: {
    sourceId: ObjectId;
    leaseToken: string;
    now: Date;
    leaseExpiresAt: Date;
  }): Promise<PostDownloadRetryItem | null> {
    const doc = await this.retries.findOneAndUpdate(
      {
        source_id: params.sourceId,
        ...buildPostDownloadRetryDueFilter(params.now),
      },
      {
        $set: {
          status: 'processing',
          lease_token: params.leaseToken,
          lease_expires_at: params.leaseExpiresAt,
          updated_at: params.now,
        },
      },
      { returnDocument: 'after' }
    );
    return doc ? { ...toRetryItem(doc), leaseToken: params.leaseToken } : null;
  }

  async completeRetry(params: {
    sourceId: ObjectId;
    leaseToken: string;
    now: Date;
  }): Promise<void> {
    await this.retries.updateOne(
      buildPostDownloadRetryOwnerFilter(params.sourceId, params.leaseToken),
      {
        $set: { status: 'completed', completed_at: params.now, updated_at: params.now, error: null },
        $unset: { lease_token: '', lease_expires_at: '', next_attempt_at: '' },
      }
    );
  }

  async deferRetry(params: {
    sourceId: ObjectId;
    leaseToken: string;
    attempts: number;
    error: string;
    now: Date;
    nextAttemptAt: Date;
  }): Promise<void> {
    await this.retries.updateOne(
      buildPostDownloadRetryOwnerFilter(params.sourceId, params.leaseToken),
      {
        $set: {
          status: 'pending',
          attempts: params.attempts,
          error: params.error,
          next_attempt_at: params.nextAttemptAt,
          updated_at: params.now,
        },
        $unset: { lease_token: '', lease_expires_at: '' },
      }
    );
  }
}

export async function createPostDownloadReconcileRepository(): Promise<MongoPostDownloadReconcileRepository> {
  const service = await getPixivImageMirrorMongoService();
  if (!service) {
    throw new Error(
      'PIXIV_IMAGE_MIRROR_MONGO_ENABLED must be true when POST_DOWNLOAD_RECONCILE_ENABLED is true'
    );
  }
  const cursor = service.getNativeCollection<CursorDocument>('pixiv_post_download_reconcile_state');
  const retries = service.getNativeCollection<RetryDocument>('pixiv_post_download_reconcile_retries');
  await retries.createIndex(
    { source_id: 1 },
    { unique: true, background: true, name: 'idx_post_download_retry_source_id' }
  );
  await retries.createIndex(
    { status: 1, next_attempt_at: 1, lease_expires_at: 1 },
    { background: true, name: 'idx_post_download_retry_due' }
  );
  return new MongoPostDownloadReconcileRepository(
    ImgCollection.getNativeCollection(),
    cursor,
    retries
  );
}

function toRetryItem(doc: RetryDocument): PostDownloadRetryItem {
  return {
    sourceId: doc.source_id,
    pixivAddr: doc.pixiv_addr,
    attempts: doc.attempts ?? 0,
  };
}
