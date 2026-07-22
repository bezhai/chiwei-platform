import { describe, expect, test } from 'bun:test';
import { loadPostDownloadReconcileConfig } from './postDownloadReconcile';

describe('loadPostDownloadReconcileConfig', () => {
  test('is disabled by default', () => {
    expect(loadPostDownloadReconcileConfig({})).toBeNull();
  });

  test('loads bounded defaults only when explicitly enabled', () => {
    expect(loadPostDownloadReconcileConfig({ POST_DOWNLOAD_RECONCILE_ENABLED: 'true' })).toEqual({
      batchSize: 20,
      retryBatchSize: 10,
      idleDelayMs: 5_000,
      retryDelayMs: 60_000,
      leaseMs: 60_000,
      epochDelayMs: 3_600_000,
    });
  });

  test('rejects non-positive limits', () => {
    expect(() => loadPostDownloadReconcileConfig({
      POST_DOWNLOAD_RECONCILE_ENABLED: '1',
      POST_DOWNLOAD_RECONCILE_BATCH_SIZE: '0',
    })).toThrow('POST_DOWNLOAD_RECONCILE_BATCH_SIZE must be a positive integer');
  });
});
