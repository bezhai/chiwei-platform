import { afterEach, beforeEach, describe, expect, it, mock } from 'bun:test';

import { checkStorageConnectivity } from './connectivity';

/**
 * 用依赖注入隔离真实 OSS / MinIO 客户端，避免 mock.module 全局污染。
 * 每个用例自己组装 deps，覆盖一种成功 / 失败组合。
 */

const MINIO_BUCKET = 'pixiv';

function makeOssDeps(listImpl: () => Promise<any>) {
    const list = mock((_query: { 'max-keys': number }, _options: unknown) => listImpl());
    return {
        list,
        getOssClient: () => ({ list }),
    };
}

function makeMinioDeps(bucketExistsImpl: (bucket: string) => Promise<boolean>) {
    const bucketExists = mock(bucketExistsImpl);
    return {
        bucketExists,
        getMinioClient: () => ({ bucketExists }),
        getMinioBucket: () => MINIO_BUCKET,
    };
}

let logSpy: ReturnType<typeof mock>;
let errorSpy: ReturnType<typeof mock>;
let originalLog: typeof console.log;
let originalError: typeof console.error;

beforeEach(() => {
    originalLog = console.log;
    originalError = console.error;
    logSpy = mock((..._args: any[]) => {});
    errorSpy = mock((..._args: any[]) => {});
    console.log = logSpy as unknown as typeof console.log;
    console.error = errorSpy as unknown as typeof console.error;
});

afterEach(() => {
    console.log = originalLog;
    console.error = originalError;
});

function logMessages(spy: ReturnType<typeof mock>): string[] {
    return spy.mock.calls.map((call) => String(call[0]));
}

describe('checkStorageConnectivity', () => {
    it('both probes succeed -> {oss:true, minio:true} with one OK log each', async () => {
        const oss = makeOssDeps(async () => ({ objects: [] }));
        const minio = makeMinioDeps(async () => true);

        const result = await checkStorageConnectivity({
            getOssClient: oss.getOssClient,
            getMinioClient: minio.getMinioClient,
            getMinioBucket: minio.getMinioBucket,
        });

        expect(result).toEqual({ oss: true, minio: true });

        // OSS 只读探测：list({'max-keys':1})
        expect(oss.list).toHaveBeenCalledTimes(1);
        expect(oss.list.mock.calls[0][0]).toEqual({ 'max-keys': 1 });

        // MinIO 只读探测：bucketExists(bucket)
        expect(minio.bucketExists).toHaveBeenCalledTimes(1);
        expect(minio.bucketExists.mock.calls[0][0]).toBe(MINIO_BUCKET);

        const logs = logMessages(logSpy);
        expect(logs.some((m) => m.includes('OSS connectivity OK'))).toBe(true);
        expect(logs.some((m) => m.includes('MinIO connectivity OK'))).toBe(true);
        expect(errorSpy).not.toHaveBeenCalled();
    });

    it('OSS list throws -> {oss:false, minio:true}, OSS FAILED logged, no throw, MinIO still probed', async () => {
        const oss = makeOssDeps(async () => {
            throw new Error('OSS endpoint unreachable');
        });
        const minio = makeMinioDeps(async () => true);

        // 函数本身不能 reject
        const result = await checkStorageConnectivity({
            getOssClient: oss.getOssClient,
            getMinioClient: minio.getMinioClient,
            getMinioBucket: minio.getMinioBucket,
        });

        expect(result).toEqual({ oss: false, minio: true });

        // 一个失败不挡另一个：MinIO 仍被探测
        expect(minio.bucketExists).toHaveBeenCalledTimes(1);

        const errors = logMessages(errorSpy);
        expect(errors.some((m) => m.includes('OSS connectivity FAILED'))).toBe(true);

        const logs = logMessages(logSpy);
        expect(logs.some((m) => m.includes('MinIO connectivity OK'))).toBe(true);
    });

    it('MinIO bucketExists throws -> {oss:true, minio:false}, MinIO FAILED logged, no throw', async () => {
        const oss = makeOssDeps(async () => ({ objects: [] }));
        const minio = makeMinioDeps(async () => {
            throw new Error('MinIO DNS lookup failed');
        });

        const result = await checkStorageConnectivity({
            getOssClient: oss.getOssClient,
            getMinioClient: minio.getMinioClient,
            getMinioBucket: minio.getMinioBucket,
        });

        expect(result).toEqual({ oss: true, minio: false });

        const errors = logMessages(errorSpy);
        expect(errors.some((m) => m.includes('MinIO connectivity FAILED'))).toBe(true);

        const logs = logMessages(logSpy);
        expect(logs.some((m) => m.includes('OSS connectivity OK'))).toBe(true);
    });

    it('MinIO bucketExists returns false -> {minio:false}, MinIO FAILED logged, no throw', async () => {
        const oss = makeOssDeps(async () => ({ objects: [] }));
        const minio = makeMinioDeps(async () => false);

        const result = await checkStorageConnectivity({
            getOssClient: oss.getOssClient,
            getMinioClient: minio.getMinioClient,
            getMinioBucket: minio.getMinioBucket,
        });

        expect(result.minio).toBe(false);

        const errors = logMessages(errorSpy);
        expect(errors.some((m) => m.includes('MinIO connectivity FAILED'))).toBe(true);
    });
});
