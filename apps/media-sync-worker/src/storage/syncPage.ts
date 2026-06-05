import { findImageByPixivAddr } from '../mongo/service';
import { syncOssObjectToMinio } from './syncToMinio';
import type { PixivImageInfo } from '../mongo/types';

/**
 * 可注入的依赖。生产用默认实现（真实 Mongo 查询 + Task1 同步），
 * 测试传入 mock —— 避免 mock.module('./syncToMinio') 污染同进程其它测试。
 */
export interface BestEffortSyncDeps {
    findImageByPixivAddr: (pixivAddr: string) => Promise<PixivImageInfo | null>;
    syncOssObjectToMinio: (key: string) => Promise<void>;
    /**
     * 单次 OSS→MinIO 同步的硬超时（毫秒）。超时按 best-effort miss 处理：
     * log 一句、不抛、不拖住 per-page 并发槽。默认 30000，可被注入覆盖（测试用）
     * 或由 env MINIO_SYNC_TIMEOUT_MS 覆盖（生产用）。
     */
    timeoutMs?: number;
}

const DEFAULT_SYNC_TIMEOUT_MS = 30000;

export type MinioSyncForTaggerResult =
    | {
        status: 'disabled';
        pixivAddr: string;
    }
    | {
        status: 'missing_key';
        pixivAddr: string;
    }
    | {
        status: 'synced';
        pixivAddr: string;
        ossKey: string;
        objectName: string;
    }
    | {
        status: 'timeout';
        pixivAddr: string;
        ossKey: string;
        objectName: string;
        timeoutMs: number;
    }
    | {
        status: 'failed';
        pixivAddr: string;
        error: string;
    };

/**
 * 同款 env flag 判定（与 index.ts 的 isEnabled 一致）：'1' 或 'true'（大小写不敏感）
 * 才算开，其它（含未设置）都算关。
 */
function isEnabled(value: string | undefined): boolean {
    return value === '1' || value?.toLowerCase() === 'true';
}

function resolveTimeoutMs(deps: BestEffortSyncDeps): number {
    if (typeof deps.timeoutMs === 'number') {
        return deps.timeoutMs;
    }
    const fromEnv = Number.parseInt(process.env.MINIO_SYNC_TIMEOUT_MS ?? '', 10);
    return Number.isNaN(fromEnv) ? DEFAULT_SYNC_TIMEOUT_MS : fromEnv;
}

const defaultDeps: BestEffortSyncDeps = {
    findImageByPixivAddr,
    syncOssObjectToMinio,
};

/** 超时哨兵：区分「同步真完成」和「等超了」。 */
const SYNC_TIMEOUT = Symbol('minio-sync-timeout');

function objectNameFromOssKey(key: string): string {
    const parts = key.split('/');
    return parts[parts.length - 1] || key;
}

export async function syncPixivToMinioForTagger(
    pixivAddr: string,
    deps: BestEffortSyncDeps = defaultDeps
): Promise<MinioSyncForTaggerResult> {
    if (!isEnabled(process.env.MINIO_SYNC_ENABLED)) {
        return { status: 'disabled', pixivAddr };
    }

    try {
        const doc = await deps.findImageByPixivAddr(pixivAddr);
        const key = doc?.tos_file_name;

        if (!key) {
            return { status: 'missing_key', pixivAddr };
        }

        const timeoutMs = resolveTimeoutMs(deps);
        const objectName = objectNameFromOssKey(key);
        const syncPromise = deps.syncOssObjectToMinio(key);
        syncPromise.catch(() => {});

        let timer: ReturnType<typeof setTimeout> | undefined;
        const timeoutPromise = new Promise<typeof SYNC_TIMEOUT>((resolve) => {
            timer = setTimeout(() => resolve(SYNC_TIMEOUT), timeoutMs);
        });

        try {
            const result = await Promise.race([syncPromise, timeoutPromise]);
            if (result === SYNC_TIMEOUT) {
                return {
                    status: 'timeout',
                    pixivAddr,
                    ossKey: key,
                    objectName,
                    timeoutMs,
                };
            }
        } finally {
            if (timer) {
                clearTimeout(timer);
            }
        }

        return {
            status: 'synced',
            pixivAddr,
            ossKey: key,
            objectName,
        };
    } catch (err) {
        return {
            status: 'failed',
            pixivAddr,
            error: err instanceof Error ? err.message : String(err),
        };
    }
}

/**
 * Best-effort 地把某张 pixiv 图从 OSS 同步进 MinIO。
 *
 * 流程：按 pixiv_addr 查 Mongo 拿代理回填的 tos_file_name（OSS object key 的唯一可信来源，
 * 不自己拼 key）→ 若拿到非空 key 则调用 Task1 的 syncOssObjectToMinio。
 *
 * best-effort 语义：整段包在 try/catch，任何失败（查库失败 / OSS 读失败 / MinIO 写失败）
 * 只 console.warn（带 pixivAddr 便于定位漏同步），绝不抛出，不拖垮下载主路径；
 * 查不到 tos_file_name（null / 空）就 log 一句跳过、也不抛。
 *
 * @param pixivAddr - Pixiv 图片名（imageUrl 最后一段），用于反查 tos_file_name
 * @param deps - 可注入依赖，默认走真实 Mongo / Task1 实现
 */
export async function bestEffortSyncToMinio(
    pixivAddr: string,
    deps: BestEffortSyncDeps = defaultDeps
): Promise<void> {
    // 安全闸：MINIO_SYNC_ENABLED 默认关。关闭时第一件事就 return —— 不查 Mongo、
    // 不读 OSS、不写 MinIO、不抛错，worker 退回纯下载。per-page 每张图都会进来，
    // 关闭时保持静默不刷屏。
    const result = await syncPixivToMinioForTagger(pixivAddr, deps);
    switch (result.status) {
        case 'disabled':
        case 'synced':
            return;
        case 'missing_key':
            console.warn(
                `跳过 MinIO 同步：pixiv_addr=${pixivAddr} 未查到 tos_file_name`
            );
            return;
        case 'timeout':
            console.warn(
                `MinIO 同步超时（best-effort，已跳过，${result.timeoutMs}ms）pixiv_addr=${pixivAddr}`
            );
            return;
        case 'failed':
            console.warn(
                `MinIO 同步失败（best-effort，已忽略）pixiv_addr=${pixivAddr}:`,
                result.error
            );
    }
}
