// LarkPhotoLibrary 的真身：一份 pixiv 抓取镜像。
//
//     元数据（地址 / 作者 / 标签 / 飞书 image_key）   另一个 Mongo 实例的 pixiv_images 集合
//     图片字节                                        MinIO 的一个桶
//
// **它连的不是业务库那个 Mongo。** 连接串走 `PIXIV_IMAGE_MONGO_*` 一族（老名字
// `PIXIV_IMAGE_MIRROR_MONGO_*` 仍然认，线上还有按那套配的），全部带默认值，所以配漏了
// 不会在启动期炸 —— 症状是第一次发图时连不上。MinIO 那三个 key 反过来：`requireEnv`
// 缺一个就抛，同样要等到第一次发图。
//
// ## 连接是懒的，而且只连一次
//
// 进程起来的时候不碰它：发图是低频功能，为它在启动期多一个连不上就起不来的理由不划算
// （拆分前也是懒连的，照搬）。连上之后客户端一直留着 —— 每次发图新建一个 MongoClient
// 等于每次都重做一遍握手和鉴权。
//
// ## 查询构造是纯函数，导出来单独测
//
// 这一层的错法都是**查回来的图不对**而不是报错，跑不到真库上没法靠"试一下"发现，所以
// 过滤条件、排序分组、翻页三样各自钉了断言（见 pixiv-mirror.test.ts）。

import * as Minio from 'minio';
import { MongoClient, type Collection, type Document, type Filter } from 'mongodb';
import { StatusMode, type ImageForLark, type ListPixivImageDto } from '@inner/pixiv-client';

import {
    dedupePixivAddrs,
    type LarkPhotoLibrary,
    type PhotoPage,
    type PhotoPageRequest,
} from './library';

// ---------------------------------------------------------------------------
// 两个后端各自的窄表面
// ---------------------------------------------------------------------------

/** Mongo 那一侧要用到的两个动作。 */
export interface PhotoDocuments {
    aggregate(
        pipeline: Document[],
        options?: { allowDiskUse?: boolean },
    ): { toArray(): Promise<Document[]> };
    updateMany(filter: Document, update: Document): Promise<unknown>;
}

/** 对象存储那一侧只有一个动作。 */
export interface PhotoObjectStore {
    read(objectName: string): Promise<Buffer>;
}

export interface PixivMirrorDeps {
    /** 都是 provider 而不是实例：连接是懒的（见文件头）。 */
    documents: () => Promise<PhotoDocuments>;
    objects: () => Promise<PhotoObjectStore>;
}

// ---------------------------------------------------------------------------
// 查询构造（纯）
// ---------------------------------------------------------------------------

/** 排序分组时临时挂上去的两列。`__` 前缀是为了不跟库里真实的列撞名。 */
const HAS_IMAGE_KEY = '__has_image_key';
const CANDIDATE_UPDATE_TIME = '__candidate_update_time';

/**
 * 状态过滤。**认不出的值按"可见且没删"处理** —— 宁可少发几张，也不要因为一个越界的
 * 枚举值把已删除的图翻出来发进群。
 */
function statusFilter(status: StatusMode): Filter<Document> | null {
    switch (status) {
        case StatusMode.NOT_DELETE:
            return { del_flag: { $ne: true } };
        case StatusMode.VISIBLE:
            return { visible: true, del_flag: { $ne: true } };
        case StatusMode.DELETE:
            return { del_flag: true };
        case StatusMode.NO_VISIBLE:
            return { visible: false, del_flag: { $ne: true } };
        case StatusMode.ALL:
            return null;
        default:
            return { visible: true, del_flag: { $ne: true } };
    }
}

function escapeRegex(value: string): string {
    return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * 一次取图问的问题翻成 Mongo 过滤条件。
 *
 * 第一条永远在：没有地址的文档拼不出「查看详情」，两个 key 都空的文档既没有 image_key
 * 也下载不到 —— 它们进了候选只会占掉一个名额然后被丢弃。
 *
 * 标签**一条一个 $and**：多个标签是"都要满足"。塞进同一个 $or 会变成"满足任一"，
 * 「发图 刻晴 原神」就会返回一堆只沾了一个词的图。
 */
export function photoFilter(query: ListPixivImageDto): Filter<Document> {
    const and: Filter<Document>[] = [
        {
            pixiv_addr: { $type: 'string', $ne: '' },
            $or: [
                { image_key: { $type: 'string', $ne: '' } },
                { tos_file_name: { $type: 'string', $ne: '' } },
            ],
        },
    ];

    const status = statusFilter(query.status);
    if (status) and.push(status);

    if (query.pixiv_addrs !== undefined) {
        and.push({ pixiv_addr: { $in: dedupePixivAddrs(query.pixiv_addrs) } });
    }

    if (query.start_time !== undefined) {
        and.push({ create_time: { $gte: new Date(query.start_time) } });
    }

    const tags = query.tag_and_author ?? query.tags ?? [];
    for (const tag of tags.filter((item) => item.trim().length > 0)) {
        // 大小写不敏感，元字符按字面量 —— 标签是用户敲进来的，`(` 不转义会让查询抛。
        const pattern = new RegExp(escapeRegex(tag.trim()), 'i');
        and.push({
            $or: [
                { author: pattern },
                { title: pattern },
                { 'multi_tags.name': pattern },
                { 'multi_tags.translation': pattern },
                { tagger_search_terms: pattern },
            ],
        });
    }

    return { $and: and };
}

interface ExplicitWindow {
    addresses: string[];
    nextOffset: number;
    exhausted: boolean;
}

/**
 * 「查看详情」那条路的翻页：地址是卡片上写死的一批，翻页就是在这个列表上取窗口。
 *
 * 非显式游标直接抛：混用两种翻页方式不会报错，只会静默取错段（详情卡片上出现的是
 * 另外几张图），跑不到真库上没法发现。
 */
function explicitWindow(request: PhotoPageRequest, limit: number): ExplicitWindow | null {
    if (request.query.pixiv_addrs === undefined) return null;
    if (request.cursor && request.cursor.mode !== 'explicit') {
        throw new Error('an explicit pixiv address query needs an explicit cursor');
    }

    const pixivAddrs = dedupePixivAddrs(request.query.pixiv_addrs);
    const offset = request.cursor
        ? request.cursor.offset
        : Math.max(0, Math.max(1, request.query.page || 1) - 1) *
          Math.max(1, request.query.page_size || 6);
    const nextOffset = Math.min(pixivAddrs.length, offset + limit);
    return {
        addresses: pixivAddrs.slice(offset, nextOffset),
        nextOffset,
        exhausted: nextOffset >= pixivAddrs.length,
    };
}

function withExcluded(
    filter: Filter<Document>,
    excluded: readonly string[] | undefined,
): Filter<Document> {
    const addresses = dedupePixivAddrs(excluded ?? []);
    if (addresses.length === 0) return filter;
    const and = Array.isArray(filter.$and) ? [...filter.$and] : [filter];
    and.push({ pixiv_addr: { $nin: addresses } });
    return { $and: and };
}

/**
 * 一次取图问的问题翻成 Mongo 聚合管线。
 *
 * **排序 → 分组的顺序是这里唯一不能动的东西**：同一个 pixiv_addr 在库里可能有好几份
 * 文档（重复抓取），只有一份带 image_key。先按"有没有 image_key"降序排，分组取第一份，
 * 挑到的就是已经传过飞书的那份。反过来的话每次都挑到没有 key 的那份，于是每次发图都
 * 重新下载、重新上传同一张图 —— 图能发出来，只是慢十倍。
 */
export function photoPipeline(request: PhotoPageRequest): Document[] {
    const limit = Math.max(1, request.limit);
    const window = explicitWindow(request, limit);
    const query = window
        ? { ...request.query, pixiv_addrs: window.addresses }
        : request.query;
    const filter = withExcluded(photoFilter(query), request.excluded);

    const pipeline: Document[] = [
        { $match: filter },
        {
            $set: {
                [HAS_IMAGE_KEY]: {
                    $cond: [
                        {
                            $and: [
                                { $eq: [{ $type: '$image_key' }, 'string'] },
                                { $ne: ['$image_key', ''] },
                            ],
                        },
                        1,
                        0,
                    ],
                },
            },
        },
        { $sort: { pixiv_addr: 1, [HAS_IMAGE_KEY]: -1, update_time: -1, _id: -1 } },
        { $group: { _id: '$pixiv_addr', __candidate: { $first: '$$ROOT' } } },
        { $replaceWith: '$__candidate' },
        { $set: { [CANDIDATE_UPDATE_TIME]: { $ifNull: ['$update_time', new Date(0)] } } },
    ];

    if (window) {
        pipeline.push({ $limit: Math.max(1, window.addresses.length) });
        return pipeline;
    }

    if (request.query.random_mode) {
        pipeline.push({ $sample: { size: limit } });
        return pipeline;
    }

    if (request.cursor) {
        if (request.cursor.mode !== 'ordered') {
            throw new Error('an ordered pixiv candidate query needs an ordered cursor');
        }
        pipeline.push({
            $match: {
                $or: [
                    { [CANDIDATE_UPDATE_TIME]: { $lt: request.cursor.updateTime } },
                    {
                        [CANDIDATE_UPDATE_TIME]: request.cursor.updateTime,
                        _id: { $lt: request.cursor.id },
                    },
                ],
            },
        });
    }

    pipeline.push({ $sort: { [CANDIDATE_UPDATE_TIME]: -1, _id: -1 } });
    // 页码只在**首页**算数：续页已经用游标定位了，再跳一次会把刚翻过去的那段又跳一遍。
    if (!request.cursor) {
        const skip = Math.max(
            0,
            (Math.max(1, request.query.page || 1) - 1) * Math.max(1, request.query.page_size || 6),
        );
        if (skip > 0) pipeline.push({ $skip: skip });
    }
    pipeline.push({ $limit: limit });
    return pipeline;
}

function textOrNothing(value: unknown): string | undefined {
    return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function numberOrNothing(value: unknown): number | undefined {
    return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

/**
 * 一份 Mongo 文档翻成一张图。**空串一律当成没有**：`image_key: ''` 会被下游当作
 * "已经传过飞书了"，然后拿空 key 去拼卡片，用户看到的是一张裂开的图。
 */
export function photoOf(doc: Document): ImageForLark | null {
    const pixivAddr = textOrNothing(doc.pixiv_addr);
    if (!pixivAddr) return null;

    return {
        author: textOrNothing(doc.author),
        image_key: textOrNothing(doc.image_key),
        pixiv_addr: pixivAddr,
        width: numberOrNothing(doc.width),
        height: numberOrNothing(doc.height),
        multi_tags: Array.isArray(doc.multi_tags)
            ? (doc.multi_tags as ImageForLark['multi_tags'])
            : undefined,
        tos_file_name: textOrNothing(doc.tos_file_name),
    };
}

/**
 * 库里存的是带前缀的路径，MinIO 桶里的对象名只有最后一段。映射错了取到的是 404，
 * 而 404 在这一层是抛出来的错误、不是空字节。
 */
export function minioObjectName(key: string): string {
    return key.split('/').pop() || key;
}

// ---------------------------------------------------------------------------
// 适配
// ---------------------------------------------------------------------------

export function pixivMirror(deps: PixivMirrorDeps): LarkPhotoLibrary {
    return {
        async candidates(request): Promise<PhotoPage> {
            const limit = Math.max(1, request.limit);
            const window = explicitWindow(request, limit);
            // 窗口已经翻到头：这一页必然是空的，一次库都不用查。
            if (window && window.addresses.length === 0) {
                return {
                    photos: [],
                    cursor: { mode: 'explicit', offset: window.nextOffset },
                    exhausted: true,
                };
            }

            const documents = await deps.documents();
            const docs = await documents
                .aggregate(photoPipeline({ ...request, limit }), { allowDiskUse: true })
                .toArray();

            if (window) {
                // 交回来的顺序必须是**卡片上那批地址的顺序**，不是库里挑出来的顺序 ——
                // 详情卡片按位置逐张对应，错位了就是张冠李戴。
                const wanted = new Set(window.addresses);
                const byAddress = new Map<string, ImageForLark>();
                for (const doc of docs) {
                    const photo = photoOf(doc);
                    if (photo && wanted.has(photo.pixiv_addr) && !byAddress.has(photo.pixiv_addr)) {
                        byAddress.set(photo.pixiv_addr, photo);
                    }
                }
                return {
                    photos: window.addresses
                        .map((pixivAddr) => byAddress.get(pixivAddr))
                        .filter((photo): photo is ImageForLark => Boolean(photo)),
                    cursor: { mode: 'explicit', offset: window.nextOffset },
                    exhausted: window.exhausted,
                };
            }

            const photos: ImageForLark[] = [];
            const seen = new Set<string>();
            for (const doc of docs) {
                const photo = photoOf(doc);
                if (photo && !seen.has(photo.pixiv_addr)) {
                    seen.add(photo.pixiv_addr);
                    photos.push(photo);
                }
            }

            // 随机模式没有稳定顺序，游标毫无意义 —— 交出去只会让调用方以为还能接着翻。
            if (request.query.random_mode) {
                return { photos, exhausted: docs.length < limit };
            }

            const last = docs.at(-1);
            return {
                photos,
                cursor: last
                    ? {
                          mode: 'ordered',
                          updateTime: last[CANDIDATE_UPDATE_TIME] ?? last.update_time ?? new Date(0),
                          id: last._id,
                      }
                    : request.cursor,
                exhausted: docs.length < limit,
            };
        },

        async bytes(tosFileName): Promise<Buffer> {
            const objects = await deps.objects();
            return objects.read(minioObjectName(tosFileName));
        },

        async noteLarkImageKey(upload): Promise<void> {
            const documents = await deps.documents();
            // **同一地址的每一份文档都要更**：只更一份的话，下次分组按"有没有 image_key"
            // 挑，挑到的可能仍是没 key 的那份，于是这张图会被反复重传。
            await documents.updateMany(
                { pixiv_addr: upload.pixiv_addr },
                {
                    $set: {
                        image_key: upload.image_key,
                        width: upload.width,
                        height: upload.height,
                        update_time: new Date(),
                    },
                },
            );
        },
    };
}

// ---------------------------------------------------------------------------
// 真实连接
// ---------------------------------------------------------------------------

function env(key: string): string | undefined {
    const value = process.env[key];
    return value && value.length > 0 ? value : undefined;
}

function intEnv(key: string): number | undefined {
    const value = env(key);
    if (!value) return undefined;
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? parsed : undefined;
}

function requireEnv(key: string): string {
    const value = env(key);
    if (!value) throw new Error(`${key} is required to read the local pixiv image mirror`);
    return value;
}

/** 老名字 `PIXIV_IMAGE_MIRROR_*` 仍然认：线上还有按那套配的。 */
function mirrorEnv(name: string): string | undefined {
    return env(`PIXIV_IMAGE_MONGO_${name}`) ?? env(`PIXIV_IMAGE_MIRROR_MONGO_${name}`);
}

function mirrorIntEnv(name: string): number | undefined {
    return intEnv(`PIXIV_IMAGE_MONGO_${name}`) ?? intEnv(`PIXIV_IMAGE_MIRROR_MONGO_${name}`);
}

function mirrorMongoUrl(): { url: string; database: string; connectTimeoutMS: number } {
    const host = mirrorEnv('HOST') ?? 'mongodb';
    const port = mirrorIntEnv('PORT') ?? 27017;
    const database = mirrorEnv('DATABASE') ?? 'chiwei_pixiv';
    const username = mirrorEnv('USERNAME') ?? env('MONGO_INITDB_ROOT_USERNAME');
    const password = mirrorEnv('PASSWORD') ?? env('MONGO_INITDB_ROOT_PASSWORD');
    const authSource = mirrorEnv('AUTH_SOURCE') ?? 'admin';
    const connectTimeoutMS = mirrorIntEnv('CONNECT_TIMEOUT_MS') ?? 2000;

    const auth =
        username && password
            ? `${encodeURIComponent(username)}:${encodeURIComponent(password)}@`
            : '';
    const params = new URLSearchParams({
        authSource,
        connectTimeoutMS: String(connectTimeoutMS),
    });
    return {
        url: `mongodb://${auth}${host}:${port}/${database}?${params.toString()}`,
        database,
        connectTimeoutMS,
    };
}

async function drain(stream: NodeJS.ReadableStream): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of stream) {
        chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    return Buffer.concat(chunks);
}

let collection: Collection<Document> | null = null;
let objectStore: PhotoObjectStore | null = null;

/**
 * 生产用的镜像。两个连接各自懒建、建好就留着（见文件头）。
 *
 * 整个进程一份：客户端池化的理由跟飞书那边一样 —— 每次发图新建一个 MongoClient 等于
 * 每次都重做握手和鉴权。
 */
export function localPixivMirror(): LarkPhotoLibrary {
    return pixivMirror({
        documents: async () => {
            if (!collection) {
                const { url, database, connectTimeoutMS } = mirrorMongoUrl();
                const client = new MongoClient(url, { connectTimeoutMS });
                await client.connect();
                collection = client.db(database).collection('pixiv_images');
                console.info(`[lark-photo] local pixiv mirror ready: database=${database}`);
            }
            return collection as unknown as PhotoDocuments;
        },
        objects: async () => {
            if (!objectStore) {
                const client = new Minio.Client({
                    endPoint: requireEnv('MINIO_ENDPOINT'),
                    port: intEnv('MINIO_PORT') ?? 9000,
                    useSSL: process.env.MINIO_USE_SSL === 'true',
                    accessKey: requireEnv('MINIO_ACCESS_KEY'),
                    secretKey: requireEnv('MINIO_SECRET_KEY'),
                });
                const bucket = env('MINIO_BUCKET') ?? 'pixiv';
                objectStore = {
                    read: async (objectName) => drain(await client.getObject(bucket, objectName)),
                };
            }
            return objectStore;
        },
    });
}
