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

let mongoClient: MongoClient | null = null;
let mongoCollection: Collection<Document> | null = null;
let minioClient: Minio.Client | null = null;

export async function getLocalPixivImages(params: ListPixivImageDto): Promise<ImageForLark[]> {
    const collection = await getPixivImageCollection();
    const filter = buildLocalPixivImageFilter(params);
    const limit = Math.max(1, params.page_size || 6);
    const skip = Math.max(0, ((params.page || 1) - 1) * limit);

    let docs: Document[];
    if (params.random_mode) {
        docs = await collection
            .aggregate([{ $match: filter }, { $sample: { size: limit } }])
            .toArray();
    } else {
        docs = await collection
            .find(filter)
            .sort({ update_time: -1, illust_id: -1, _id: -1 })
            .skip(skip)
            .limit(limit)
            .toArray();
    }

    const images = docs
        .map(mapLocalPixivImageDoc)
        .filter((image): image is ImageForLark => !!image);
    if (params.pixiv_addrs?.length) {
        const order = new Map(params.pixiv_addrs.map((addr, index) => [addr, index]));
        images.sort((a, b) => (order.get(a.pixiv_addr) ?? 0) - (order.get(b.pixiv_addr) ?? 0));
    }
    return images;
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
    const and: Filter<Document>[] = [];

    const statusFilter = buildStatusFilter(params.status);
    if (statusFilter) {
        and.push(statusFilter);
    }

    if (params.pixiv_addrs?.length) {
        and.push({ pixiv_addr: { $in: params.pixiv_addrs } });
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
            ],
        });
    }

    return and.length === 0 ? {} : { $and: and };
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
