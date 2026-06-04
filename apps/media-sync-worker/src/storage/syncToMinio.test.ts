import { beforeEach, describe, expect, it, mock } from 'bun:test';

// ---- mock OSS client module ----
let ossGetImpl: (fileName: string) => Promise<any> = async () => ({ content: Buffer.from('') });
const ossGet = mock((fileName: string) => ossGetImpl(fileName));

mock.module('../oss/client', () => ({
    getOssClient: () => ({
        get: ossGet,
    }),
}));

// ---- mock MinIO client module ----
let statImpl: (bucket: string, key: string) => Promise<any> = async () => ({});
let putImpl: (bucket: string, key: string, buf: Buffer) => Promise<any> = async () => ({});
const statObject = mock((bucket: string, key: string) => statImpl(bucket, key));
const putObject = mock((bucket: string, key: string, buf: Buffer) => putImpl(bucket, key, buf));

const MINIO_BUCKET = 'pixiv';

mock.module('../minio/client', () => ({
    getMinioClient: () => ({
        statObject,
        putObject,
    }),
    getMinioBucket: () => MINIO_BUCKET,
}));

const { syncOssObjectToMinio } = await import('./syncToMinio');

function resetMocks() {
    ossGet.mockClear();
    statObject.mockClear();
    putObject.mockClear();
    // default: OSS returns bytes, object does NOT exist in MinIO, put succeeds
    ossGetImpl = async () => ({ content: Buffer.from('IMAGE-BYTES') });
    statImpl = async () => {
        const err: any = new Error('Not Found');
        err.code = 'NotFound';
        throw err;
    };
    putImpl = async () => ({ etag: 'etag-123' });
}

describe('syncOssObjectToMinio', () => {
    beforeEach(() => {
        resetMocks();
    });

    it('happy path: reads from OSS, finds object missing, writes to MinIO with correct bucket/key/bytes', async () => {
        const bytes = Buffer.from('HELLO-IMAGE');
        ossGetImpl = async () => ({ content: bytes });

        await syncOssObjectToMinio('123_p0.png');

        // OSS read with the given key
        expect(ossGet).toHaveBeenCalledTimes(1);
        expect(ossGet.mock.calls[0][0]).toBe('123_p0.png');

        // existence check on the pixiv bucket with the same key
        expect(statObject).toHaveBeenCalledTimes(1);
        expect(statObject.mock.calls[0][0]).toBe('pixiv');
        expect(statObject.mock.calls[0][1]).toBe('123_p0.png');

        // put with bucket / key / exact bytes
        expect(putObject).toHaveBeenCalledTimes(1);
        expect(putObject.mock.calls[0][0]).toBe('pixiv');
        expect(putObject.mock.calls[0][1]).toBe('123_p0.png');
        expect(putObject.mock.calls[0][2]).toBe(bytes);
    });

    it('path-prefixed key: OSS read uses full key, MinIO stat/put use basename only', async () => {
        const bytes = Buffer.from('HELLO-IMAGE');
        ossGetImpl = async () => ({ content: bytes });

        await syncOssObjectToMinio('pixiv_img_v2/20260604/123_p0.png');

        // OSS read with the FULL path-prefixed key (OSS object lives at that path)
        expect(ossGet).toHaveBeenCalledTimes(1);
        expect(ossGet.mock.calls[0][0]).toBe('pixiv_img_v2/20260604/123_p0.png');

        // existence check on the pixiv bucket with the BASENAME only (flat MinIO layout)
        expect(statObject).toHaveBeenCalledTimes(1);
        expect(statObject.mock.calls[0][0]).toBe('pixiv');
        expect(statObject.mock.calls[0][1]).toBe('123_p0.png');

        // put with bucket / basename key / exact bytes
        expect(putObject).toHaveBeenCalledTimes(1);
        expect(putObject.mock.calls[0][0]).toBe('pixiv');
        expect(putObject.mock.calls[0][1]).toBe('123_p0.png');
        expect(putObject.mock.calls[0][2]).toBe(bytes);
    });

    it('idempotent with path-prefixed key: statObject checks basename, skips when present', async () => {
        statImpl = async () => ({ size: 42, etag: 'existing' }); // exists -> no throw

        await syncOssObjectToMinio('pixiv_img_v2/20260604/123_p0.png');

        expect(statObject).toHaveBeenCalledTimes(1);
        expect(statObject.mock.calls[0][1]).toBe('123_p0.png');
        expect(putObject).not.toHaveBeenCalled();
        expect(ossGet).not.toHaveBeenCalled();
    });

    it('idempotent: object already exists in MinIO, skips putObject and returns normally', async () => {
        statImpl = async () => ({ size: 42, etag: 'existing' }); // exists -> no throw

        await syncOssObjectToMinio('123_p0.png');

        expect(statObject).toHaveBeenCalledTimes(1);
        expect(putObject).not.toHaveBeenCalled();
        // OSS read must NOT happen when object already present (no point downloading)
        expect(ossGet).not.toHaveBeenCalled();
    });

    it('propagates OSS read failure (does not swallow)', async () => {
        ossGetImpl = async () => {
            throw new Error('OSS boom');
        };

        await expect(syncOssObjectToMinio('123_p0.png')).rejects.toThrow('OSS boom');
        expect(putObject).not.toHaveBeenCalled();
    });

    it('propagates MinIO putObject failure (does not swallow)', async () => {
        putImpl = async () => {
            throw new Error('MinIO put boom');
        };

        await expect(syncOssObjectToMinio('123_p0.png')).rejects.toThrow('MinIO put boom');
        expect(putObject).toHaveBeenCalledTimes(1);
    });

    it('treats statObject 404 (statusCode, no code) as not-found -> reads OSS, writes MinIO', async () => {
        statImpl = async () => {
            const err: any = new Error('Not Found');
            err.statusCode = 404; // gateway / SDK variant: only statusCode, no code field
            throw err;
        };
        const bytes = Buffer.from('IMAGE-BYTES');
        ossGetImpl = async () => ({ content: bytes });

        await syncOssObjectToMinio('123_p0.png');

        expect(ossGet).toHaveBeenCalledTimes(1);
        expect(putObject).toHaveBeenCalledTimes(1);
        expect(putObject.mock.calls[0][2]).toBe(bytes);
    });

    it('treats statObject 404 ($metadata.httpStatusCode, no code) as not-found -> reads OSS, writes MinIO', async () => {
        statImpl = async () => {
            const err: any = new Error('Not Found');
            err.$metadata = { httpStatusCode: 404 }; // AWS-SDK-style metadata, no code field
            throw err;
        };

        await syncOssObjectToMinio('123_p0.png');

        expect(ossGet).toHaveBeenCalledTimes(1);
        expect(putObject).toHaveBeenCalledTimes(1);
    });

    it('propagates a real non-404 statObject error (e.g. AccessDenied) -> does NOT write', async () => {
        statImpl = async () => {
            const err: any = new Error('Access Denied');
            err.code = 'AccessDenied';
            throw err;
        };

        await expect(syncOssObjectToMinio('123_p0.png')).rejects.toThrow('Access Denied');
        expect(ossGet).not.toHaveBeenCalled();
        expect(putObject).not.toHaveBeenCalled();
    });
});
