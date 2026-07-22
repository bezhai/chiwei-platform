import { loadPostDownloadReconcileConfig } from '../config/postDownloadReconcile';
import { createPostDownloadReconcileRepository } from '../mongo/postDownloadReconcileRepository';
import {
  startPostDownloadReconcileWorker,
  type PostDownloadReconcileWorker,
} from './postDownloadReconciler';

let worker: PostDownloadReconcileWorker | null = null;

export async function initPostDownloadReconcileRuntime(): Promise<void> {
  const config = loadPostDownloadReconcileConfig();
  if (!config) {
    console.log('Post-download historical reconciler disabled.');
    return;
  }
  if (worker) return;

  const repository = await createPostDownloadReconcileRepository();
  worker = startPostDownloadReconcileWorker({ repository, config });
  console.log(
    `Post-download historical reconciler started: batch_size=${config.batchSize} `
    + `retry_batch_size=${config.retryBatchSize} epoch_delay_ms=${config.epochDelayMs}`
  );
}

export async function stopPostDownloadReconcileRuntime(): Promise<void> {
  await worker?.stop();
  worker = null;
}
