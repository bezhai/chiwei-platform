import { describe, expect, test } from 'bun:test';
import { ObjectId } from 'mongodb';
import {
  buildPostDownloadCursorOwnerFilter,
  buildPostDownloadRetryDueFilter,
  buildPostDownloadRetryOwnerFilter,
  buildPostDownloadRetryWritableFilter,
} from './postDownloadReconcileRepository';

describe('post-download reconcile fencing filters', () => {
  test('cursor mutations require the current lease token', () => {
    expect(buildPostDownloadCursorOwnerFilter('owner-2')).toEqual({
      _id: 'post_download_reconciler',
      lease_token: 'owner-2',
    });
  });

  test('retry mutations require both source identity and lease token', () => {
    const sourceId = new ObjectId('000000000000000000000001');
    expect(buildPostDownloadRetryOwnerFilter(sourceId, 'retry-owner')).toEqual({
      source_id: sourceId,
      status: 'processing',
      lease_token: 'retry-owner',
    });
  });

  test('expired processing retries become claimable after a pod dies', () => {
    const now = new Date('2026-07-22T08:00:00.000Z');
    expect(buildPostDownloadRetryDueFilter(now)).toEqual({
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
    });
  });

  test('cursor replay cannot reset a currently owned retry lease', () => {
    const sourceId = new ObjectId('000000000000000000000001');
    const now = new Date('2026-07-22T08:00:00.000Z');
    expect(buildPostDownloadRetryWritableFilter(sourceId, now)).toEqual({
      source_id: sourceId,
      $or: [
        { status: { $ne: 'processing' } },
        { lease_expires_at: { $lte: now } },
      ],
    });
  });
});
