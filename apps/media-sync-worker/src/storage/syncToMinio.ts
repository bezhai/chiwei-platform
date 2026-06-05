import { getMinioBucket, getMinioClient } from '../minio/client';
import { getOssClient } from '../oss/client';

/**
 * 把一个 OSS 对象同步进自建 MinIO 的 pixiv bucket（内网训练缓存）。
 *
 * 语义：
 * - 给定 OSS object key（即 Mongo 里的 tos_file_name，带路径前缀，如
 *   `pixiv_img_v2/20260604/12345678_p0.png`），先查 MinIO 是否已存在该对象；
 *   已存在则直接跳过、正常返回（幂等，不重复读 OSS、不重复写）。
 * - 不存在则从 OSS 读出字节、写进 MinIO 的 pixiv bucket。
 *
 * key 用法（不对称）：
 * - OSS 读用 **完整带路径的 key**——OSS 对象就在那个路径下。
 * - MinIO 的 statObject / putObject 用 key 的 **basename**（最后一段文件名）。
 *   pixiv 文件名含 illust_id+页码全局唯一、扁平存放无碰撞，训练脚本按文件名直接取，
 *   所以 MinIO 不带 `pixiv_img_v2/日期/` 前缀。
 *
 * 错误语义（best-effort 的吞错由调用方负责，本函数保持清晰语义）：
 * - 真实失败（OSS 读失败、MinIO 写失败）一律抛出，不在本函数 try/catch 吞掉。
 * - “对象已存在”不是失败，正常返回（跳过）。
 *
 * @param key OSS object key（带路径前缀）；MinIO 侧用其 basename
 */
export async function syncOssObjectToMinio(key: string): Promise<void> {
    const bucket = getMinioBucket();
    const minio = getMinioClient();
    const minioKey = minioObjectName(key);

    if (await minioObjectExists(minio, bucket, minioKey)) {
        console.info(
            `MinIO 对象已存在，跳过同步：bucket=${bucket} object=${minioKey} oss_key=${key}`
        );
        return;
    }

    const ossObject = await getOssClient().get(key);
    const content: Buffer = ossObject.content;

    await minio.putObject(bucket, minioKey, content);
    console.info(`MinIO 同步成功：bucket=${bucket} object=${minioKey} oss_key=${key}`);
}

/**
 * 从带路径前缀的 OSS key 取出 MinIO 侧扁平存放用的 object name（basename）。
 * 防御：basename 为空（理论不会，如 key 以 `/` 结尾）时回退用原 key，
 * 避免写出空 object name。
 */
function minioObjectName(key: string): string {
    const basename = key.split('/').pop();
    return basename ? basename : key;
}

/**
 * 判断 MinIO 里是否已存在某对象。
 *
 * MinIO SDK 的 statObject 在对象不存在时会 throw，据此判断：throw 即视为不存在。
 * 其它异常（网络 / 权限等）原样抛出，交给调用方处理。
 */
async function minioObjectExists(
    minio: ReturnType<typeof getMinioClient>,
    bucket: string,
    key: string,
): Promise<boolean> {
    try {
        await minio.statObject(bucket, key);
        return true;
    } catch (err: any) {
        if (isObjectNotFound(err)) {
            return false;
        }
        throw err;
    }
}

function isObjectNotFound(err: any): boolean {
    const code = err?.code;
    if (code === 'NotFound' || code === 'NoSuchKey') {
        return true;
    }
    // 某些网关 / SDK 版本只给 HTTP 404、不带 code，仍是「对象不存在」，
    // 不识别会被误判成真实故障 → syncOssObjectToMinio 抛错 → 那张图漏同步。
    if (err?.statusCode === 404) {
        return true;
    }
    if (err?.$metadata?.httpStatusCode === 404) {
        return true;
    }
    return false;
}
