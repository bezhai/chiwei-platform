import { getMinioBucket, getMinioClient } from '../minio/client';
import { getOssClient } from '../oss/client';

/**
 * 存储连通性自检结果。每个外部依赖一个 boolean，true=探测通过、false=探测失败。
 * 注意：探测失败只反映在 boolean 上，本检查不抛错——冒烟时即使某连接不通，
 * pod 也别 crashloop，错误要清楚打在日志里供人看。
 */
export interface StorageConnectivityResult {
    oss: boolean;
    minio: boolean;
}

/**
 * 探测只用到的最小能力面：OSS 只需 list，MinIO 只需 bucketExists。
 * 用窄接口而非完整 OSS / Minio.Client 类型，单测注入 mock 时不用实现整套接口。
 */
interface OssProbeClient {
    list(query: { 'max-keys': number }, options: unknown): Promise<unknown>;
}

interface MinioProbeClient {
    bucketExists(bucket: string): Promise<boolean>;
}

/**
 * 可注入依赖，默认走真实客户端。单测注入 mock，避免连真实 OSS / MinIO，
 * 也避免 mock.module 全局污染别的测试文件。
 */
export interface StorageConnectivityDeps {
    getOssClient: () => OssProbeClient;
    getMinioClient: () => MinioProbeClient;
    getMinioBucket: () => string;
}

const DEFAULT_DEPS: StorageConnectivityDeps = {
    getOssClient,
    getMinioClient,
    getMinioBucket,
};

/**
 * 对 OSS 和 MinIO 各做一次只读探测，用于泳道部署时的冒烟验证
 * （不跑下载消费循环也能确认外部连接通）。
 *
 * - OSS：`list({'max-keys':1})` 列 1 个对象，验证 endpoint + 凭证 + bucket 通。
 * - MinIO：`bucketExists(bucket)` 验证 DNS + 凭证 + bucket；返回 false 或 throw 都算失败。
 *
 * 每个依赖各自 try/catch、各自打清晰日志，一个失败不挡另一个；本函数永不抛错，
 * 把成功 / 失败收集进 {oss, minio} 返回，由调用方决定怎么处理。
 */
export async function checkStorageConnectivity(
    deps: StorageConnectivityDeps = DEFAULT_DEPS,
): Promise<StorageConnectivityResult> {
    const oss = await probeOss(deps);
    const minio = await probeMinio(deps);
    return { oss, minio };
}

async function probeOss(deps: StorageConnectivityDeps): Promise<boolean> {
    try {
        await deps.getOssClient().list({ 'max-keys': 1 }, {});
        console.log('OSS connectivity OK');
        return true;
    } catch (err) {
        console.error('OSS connectivity FAILED:', err);
        return false;
    }
}

async function probeMinio(deps: StorageConnectivityDeps): Promise<boolean> {
    const bucket = deps.getMinioBucket();
    try {
        const exists = await deps.getMinioClient().bucketExists(bucket);
        if (!exists) {
            console.error(`MinIO connectivity FAILED: bucket "${bucket}" not found`);
            return false;
        }
        console.log(`MinIO connectivity OK (bucket=${bucket})`);
        return true;
    } catch (err) {
        console.error('MinIO connectivity FAILED:', err);
        return false;
    }
}
