import * as Minio from 'minio';
import { MongoClient, type Collection, type Document, type Filter } from 'mongodb';
import { StatusMode } from 'types/pixiv';
import type { ImageForLark, ListPixivImageDto, ReportLarkUploadDto } from 'types/pixiv';

interface LocalPixivMongoConfig {
    host: string;
    port: number;
    database: string;
    username?: string;
    password?: string;
    authSource: string;
    connectTimeoutMS: number;
}

interface LocalPixivCandidateCollection {
    aggregate(
        pipeline: Document[],
        options?: { allowDiskUse?: boolean },
    ): { toArray(): Promise<Document[]> };
}

export type LocalPixivCandidateCursor =
    | {
          mode: 'ordered';
          updateTime: unknown;
          id: unknown;
      }
    | {
          mode: 'explicit';
          offset: number;
      };

export interface LocalPixivCandidateRequest {
    params: ListPixivImageDto;
    limit: number;
    cursor?: LocalPixivCandidateCursor;
    excludedPixivAddrs?: readonly string[];
}

export interface LocalPixivCandidatePage {
    images: ImageForLark[];
    cursor?: LocalPixivCandidateCursor;
    exhausted: boolean;
}

interface LocalPixivCandidateDependencies {
    collection?: LocalPixivCandidateCollection;
}

let mongoClient: MongoClient | null = null;
let mongoCollection: Collection<Document> | null = null;
let minioClient: Minio.Client | null = null;

export async function getLocalPixivImages(params: ListPixivImageDto): Promise<ImageForLark[]> {
    const limit = Math.max(1, params.page_size || 6);
    return (await getLocalPixivImageCandidates({ params, limit })).images;
}

export async function getLocalPixivImageCandidates(
    request: LocalPixivCandidateRequest,
    dependencies: LocalPixivCandidateDependencies = {},
): Promise<LocalPixivCandidatePage> {
    const limit = Math.max(1, request.limit);
    const explicitWindow = getExplicitWindow(request, limit);
    if (explicitWindow && explicitWindow.addresses.length === 0) {
        return {
            images: [],
            cursor: { mode: 'explicit', offset: explicitWindow.nextOffset },
            exhausted: true,
        };
    }

    const collection = dependencies.collection ?? (await getPixivImageCollection());
    const docs = await collection
        .aggregate(buildLocalPixivCandidatePipeline({ ...request, limit }), {
            allowDiskUse: true,
        })
        .toArray();

    if (explicitWindow) {
        const allowed = new Set(explicitWindow.addresses);
        const byAddress = new Map<string, ImageForLark>();
        for (const doc of docs) {
            const image = mapLocalPixivImageDoc(doc);
            if (image && allowed.has(image.pixiv_addr) && !byAddress.has(image.pixiv_addr)) {
                byAddress.set(image.pixiv_addr, image);
            }
        }
        const images = explicitWindow.addresses
            .map((pixivAddr) => byAddress.get(pixivAddr))
            .filter((image): image is ImageForLark => !!image);
        return {
            images,
            cursor: { mode: 'explicit', offset: explicitWindow.nextOffset },
            exhausted: explicitWindow.exhausted,
        };
    }

    const images: ImageForLark[] = [];
    const seen = new Set<string>();
    for (const doc of docs) {
        const image = mapLocalPixivImageDoc(doc);
        if (image && !seen.has(image.pixiv_addr)) {
            seen.add(image.pixiv_addr);
            images.push(image);
        }
    }

    if (request.params.random_mode) {
        return {
            images,
            exhausted: docs.length < limit,
        };
    }

    const lastDoc = docs.at(-1);
    return {
        images,
        cursor: lastDoc
            ? {
                  mode: 'ordered',
                  updateTime: lastDoc.__candidate_update_time ?? lastDoc.update_time ?? new Date(0),
                  id: lastDoc._id,
              }
            : request.cursor,
        exhausted: docs.length < limit,
    };
}

export function buildLocalPixivCandidatePipeline(request: LocalPixivCandidateRequest): Document[] {
    const limit = Math.max(1, request.limit);
    const explicitWindow = getExplicitWindow(request, limit);
    const params = explicitWindow
        ? { ...request.params, pixiv_addrs: explicitWindow.addresses }
        : request.params;
    const filter = addExcludedPixivAddrs(
        buildLocalPixivImageFilter(params),
        request.excludedPixivAddrs,
    );
    const pipeline: Document[] = [
        { $match: filter },
        {
            $set: {
                __has_image_key: {
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
        {
            $sort: {
                pixiv_addr: 1,
                __has_image_key: -1,
                update_time: -1,
                _id: -1,
            },
        },
        { $group: { _id: '$pixiv_addr', __candidate: { $first: '$$ROOT' } } },
        { $replaceWith: '$__candidate' },
        {
            $set: {
                __candidate_update_time: { $ifNull: ['$update_time', new Date(0)] },
            },
        },
    ];

    if (explicitWindow) {
        pipeline.push({ $limit: Math.max(1, explicitWindow.addresses.length) });
        return pipeline;
    }

    if (request.params.random_mode) {
        pipeline.push({ $sample: { size: limit } });
        return pipeline;
    }

    if (request.cursor) {
        if (request.cursor.mode !== 'ordered') {
            throw new Error('ordered pixiv candidate query requires an ordered cursor');
        }
        pipeline.push({
            $match: {
                $or: [
                    { __candidate_update_time: { $lt: request.cursor.updateTime } },
                    {
                        __candidate_update_time: request.cursor.updateTime,
                        _id: { $lt: request.cursor.id },
                    },
                ],
            },
        });
    }

    pipeline.push({ $sort: { __candidate_update_time: -1, _id: -1 } });
    if (!request.cursor) {
        const skip = Math.max(
            0,
            (Math.max(1, request.params.page || 1) - 1) *
                Math.max(1, request.params.page_size || 6),
        );
        if (skip > 0) pipeline.push({ $skip: skip });
    }
    pipeline.push({ $limit: limit });
    return pipeline;
}

export async function getLocalPixivImageContent(tosFileName: string): Promise<Buffer> {
    const stream = await getMinioClient().getObject(getMinioBucket(), minioObjectName(tosFileName));
    return streamToBuffer(stream);
}

export async function reportLocalLarkUpload(params: ReportLarkUploadDto): Promise<void> {
    const collection = await getPixivImageCollection();
    await collection.updateMany(
        { pixiv_addr: params.pixiv_addr },
        {
            $set: {
                image_key: params.image_key,
                width: params.width,
                height: params.height,
                update_time: new Date(),
            },
        },
    );
}

export function buildLocalPixivImageFilter(params: ListPixivImageDto): Filter<Document> {
    const and: Filter<Document>[] = [
        {
            pixiv_addr: { $type: 'string', $ne: '' },
            $or: [
                { image_key: { $type: 'string', $ne: '' } },
                { tos_file_name: { $type: 'string', $ne: '' } },
            ],
        },
    ];

    const statusFilter = buildStatusFilter(params.status);
    if (statusFilter) {
        and.push(statusFilter);
    }

    if (params.pixiv_addrs !== undefined) {
        and.push({ pixiv_addr: { $in: dedupePixivAddrs(params.pixiv_addrs) } });
    }

    if (params.start_time !== undefined) {
        and.push({ create_time: { $gte: new Date(params.start_time) } });
    }

    const tags = params.tag_and_author ?? params.tags ?? [];
    for (const tag of tags.filter((item) => item.trim().length > 0)) {
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

export function dedupePixivAddrs(pixivAddrs: readonly string[]): string[] {
    const seen = new Set<string>();
    const result: string[] = [];
    for (const pixivAddr of pixivAddrs) {
        if (pixivAddr.length === 0 || seen.has(pixivAddr)) continue;
        seen.add(pixivAddr);
        result.push(pixivAddr);
    }
    return result;
}

function getExplicitWindow(
    request: LocalPixivCandidateRequest,
    limit: number,
): {
    addresses: string[];
    nextOffset: number;
    exhausted: boolean;
} | null {
    if (request.params.pixiv_addrs === undefined) return null;
    if (request.cursor && request.cursor.mode !== 'explicit') {
        throw new Error('explicit pixiv candidate query requires an explicit cursor');
    }

    const pixivAddrs = dedupePixivAddrs(request.params.pixiv_addrs);
    const offset = request.cursor
        ? request.cursor.offset
        : Math.max(0, Math.max(1, request.params.page || 1) - 1) *
          Math.max(1, request.params.page_size || 6);
    const nextOffset = Math.min(pixivAddrs.length, offset + limit);
    return {
        addresses: pixivAddrs.slice(offset, nextOffset),
        nextOffset,
        exhausted: nextOffset >= pixivAddrs.length,
    };
}

function addExcludedPixivAddrs(
    filter: Filter<Document>,
    excludedPixivAddrs: readonly string[] | undefined,
): Filter<Document> {
    const excluded = dedupePixivAddrs(excludedPixivAddrs ?? []);
    if (excluded.length === 0) return filter;
    const and = Array.isArray(filter.$and) ? [...filter.$and] : [filter];
    and.push({ pixiv_addr: { $nin: excluded } });
    return { $and: and };
}

export function mapLocalPixivImageDoc(doc: Document): ImageForLark | null {
    const pixivAddr = stringOrUndefined(doc.pixiv_addr);
    if (!pixivAddr) return null;

    return {
        author: stringOrUndefined(doc.author),
        image_key: stringOrUndefined(doc.image_key),
        pixiv_addr: pixivAddr,
        width: numberOrUndefined(doc.width),
        height: numberOrUndefined(doc.height),
        multi_tags: Array.isArray(doc.multi_tags)
            ? (doc.multi_tags as ImageForLark['multi_tags'])
            : undefined,
        tos_file_name: stringOrUndefined(doc.tos_file_name),
    };
}

export function minioObjectName(key: string): string {
    const basename = key.split('/').pop();
    return basename ? basename : key;
}

function buildStatusFilter(status: StatusMode): Filter<Document> | null {
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

async function getPixivImageCollection(): Promise<Collection<Document>> {
    if (!mongoCollection) {
        const config = loadLocalPixivMongoConfig();
        mongoClient = new MongoClient(buildMongoUrl(config), {
            connectTimeoutMS: config.connectTimeoutMS,
        });
        await mongoClient.connect();
        mongoCollection = mongoClient.db(config.database).collection('pixiv_images');
        console.info(
            `Local pixiv image Mongo ready: host=${config.host} database=${config.database}`,
        );
    }
    return mongoCollection;
}

function loadLocalPixivMongoConfig(): LocalPixivMongoConfig {
    return {
        host: env('PIXIV_IMAGE_MONGO_HOST') ?? env('PIXIV_IMAGE_MIRROR_MONGO_HOST') ?? 'mongodb',
        port: intEnv('PIXIV_IMAGE_MONGO_PORT') ?? intEnv('PIXIV_IMAGE_MIRROR_MONGO_PORT') ?? 27017,
        database:
            env('PIXIV_IMAGE_MONGO_DATABASE') ??
            env('PIXIV_IMAGE_MIRROR_MONGO_DATABASE') ??
            'chiwei_pixiv',
        username:
            env('PIXIV_IMAGE_MONGO_USERNAME') ??
            env('PIXIV_IMAGE_MIRROR_MONGO_USERNAME') ??
            env('MONGO_INITDB_ROOT_USERNAME'),
        password:
            env('PIXIV_IMAGE_MONGO_PASSWORD') ??
            env('PIXIV_IMAGE_MIRROR_MONGO_PASSWORD') ??
            env('MONGO_INITDB_ROOT_PASSWORD'),
        authSource:
            env('PIXIV_IMAGE_MONGO_AUTH_SOURCE') ??
            env('PIXIV_IMAGE_MIRROR_MONGO_AUTH_SOURCE') ??
            'admin',
        connectTimeoutMS:
            intEnv('PIXIV_IMAGE_MONGO_CONNECT_TIMEOUT_MS') ??
            intEnv('PIXIV_IMAGE_MIRROR_MONGO_CONNECT_TIMEOUT_MS') ??
            2000,
    };
}

function buildMongoUrl(config: LocalPixivMongoConfig): string {
    const auth =
        config.username && config.password
            ? `${encodeURIComponent(config.username)}:${encodeURIComponent(config.password)}@`
            : '';
    const params = new URLSearchParams({
        authSource: config.authSource,
        connectTimeoutMS: String(config.connectTimeoutMS),
    });
    return `mongodb://${auth}${config.host}:${config.port}/${config.database}?${params.toString()}`;
}

function getMinioClient(): Minio.Client {
    if (!minioClient) {
        const endPoint = requireEnv('MINIO_ENDPOINT');
        minioClient = new Minio.Client({
            endPoint,
            port: intEnv('MINIO_PORT') ?? 9000,
            useSSL: process.env.MINIO_USE_SSL === 'true',
            accessKey: requireEnv('MINIO_ACCESS_KEY'),
            secretKey: requireEnv('MINIO_SECRET_KEY'),
        });
    }
    return minioClient;
}

function getMinioBucket(): string {
    return env('MINIO_BUCKET') ?? 'pixiv';
}

async function streamToBuffer(stream: NodeJS.ReadableStream): Promise<Buffer> {
    const chunks: Buffer[] = [];
    for await (const chunk of stream) {
        chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    return Buffer.concat(chunks);
}

function env(key: string): string | undefined {
    const value = process.env[key];
    return value && value.length > 0 ? value : undefined;
}

function requireEnv(key: string): string {
    const value = env(key);
    if (!value) {
        throw new Error(`${key} is required for local pixiv image source`);
    }
    return value;
}

function intEnv(key: string): number | undefined {
    const value = env(key);
    if (!value) return undefined;
    const parsed = Number.parseInt(value, 10);
    return Number.isFinite(parsed) ? parsed : undefined;
}

function escapeRegex(value: string): string {
    return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function stringOrUndefined(value: unknown): string | undefined {
    return typeof value === 'string' && value.length > 0 ? value : undefined;
}

function numberOrUndefined(value: unknown): number | undefined {
    return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}
