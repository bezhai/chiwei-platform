type Env = Record<string, string | undefined>;

export interface PostDownloadReconcileConfig {
  batchSize: number;
  retryBatchSize: number;
  idleDelayMs: number;
  retryDelayMs: number;
  leaseMs: number;
  epochDelayMs: number;
}

function isEnabled(value: string | undefined): boolean {
  return value === '1' || value?.toLowerCase() === 'true';
}

function positiveInt(env: Env, name: string, fallback: number): number {
  const raw = env[name];
  if (raw === undefined || raw === '') return fallback;
  const parsed = Number.parseInt(raw, 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new Error(`${name} must be a positive integer`);
  }
  return parsed;
}

export function loadPostDownloadReconcileConfig(
  env: Env = process.env
): PostDownloadReconcileConfig | null {
  if (!isEnabled(env.POST_DOWNLOAD_RECONCILE_ENABLED)) return null;

  return {
    batchSize: positiveInt(env, 'POST_DOWNLOAD_RECONCILE_BATCH_SIZE', 20),
    retryBatchSize: positiveInt(env, 'POST_DOWNLOAD_RECONCILE_RETRY_BATCH_SIZE', 10),
    idleDelayMs: positiveInt(env, 'POST_DOWNLOAD_RECONCILE_IDLE_DELAY_MS', 5_000),
    retryDelayMs: positiveInt(env, 'POST_DOWNLOAD_RECONCILE_RETRY_DELAY_MS', 60_000),
    leaseMs: positiveInt(env, 'POST_DOWNLOAD_RECONCILE_LEASE_MS', 60_000),
    epochDelayMs: positiveInt(env, 'POST_DOWNLOAD_RECONCILE_EPOCH_DELAY_MS', 3_600_000),
  };
}
