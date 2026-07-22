import type { Collection, Document } from 'mongodb';
import { MongoService } from '@inner/shared/mongo';
import { ImgCollection } from './client';
import { loadPixivImageMirrorMongoConfig } from './imageMirrorConfig';
import { buildPixivImageMirrorOperations } from './imageMirrorOperations';

export type PixivImageMirrorResult =
    | { status: 'disabled' }
    | { status: 'missing_source' }
    | { status: 'synced'; count: number };

interface MirrorState {
    service: MongoService;
    collection: Collection<Document>;
}

let mirrorStatePromise: Promise<MirrorState | null> | null = null;

async function getMirrorState(): Promise<MirrorState | null> {
    if (!mirrorStatePromise) {
        mirrorStatePromise = createMirrorState().catch((err) => {
            mirrorStatePromise = null;
            throw err;
        });
    }
    return mirrorStatePromise;
}

export async function getPixivImageMirrorCollection(): Promise<Collection<Document> | null> {
    return (await getMirrorState())?.collection ?? null;
}

export async function getPixivImageMirrorMongoService(): Promise<MongoService | null> {
    return (await getMirrorState())?.service ?? null;
}

async function createMirrorState(): Promise<MirrorState | null> {
    const config = loadPixivImageMirrorMongoConfig();
    if (!config) {
        return null;
    }

    const service = new MongoService(config);
    await service.initialize();
    const collection = service.getNativeCollection<Document>('pixiv_images');
    await ensurePixivImageMirrorIndexes(collection);
    console.log(`Pixiv image mirror Mongo ready: host=${config.host} database=${config.database}`);
    return { service, collection };
}

async function ensurePixivImageMirrorIndexes(collection: Collection<Document>): Promise<void> {
    await collection.createIndex({ pixiv_addr: 1 }, { background: true, name: 'idx_pixiv_images_pixiv_addr' });
    await collection.createIndex({ illust_id: -1 }, { background: true, name: 'idx_pixiv_images_illust_id_desc' });
    await collection.createIndex(
        { update_time: 1, _id: 1 },
        { background: true, name: 'idx_pixiv_images_update_time_id' }
    );
    await collection.createIndex({ tos_file_name: 1 }, { background: true, name: 'idx_pixiv_images_tos_file_name' });
    await collection.createIndex(
        { 'multi_tags.name': 1 },
        { background: true, name: 'idx_pixiv_images_multi_tags_name' }
    );
}

export async function syncPixivImageToLocal(pixivAddr: string): Promise<PixivImageMirrorResult> {
    const state = await getMirrorState();
    if (!state) {
        return { status: 'disabled' };
    }

    const docs = await ImgCollection.find({ pixiv_addr: pixivAddr });
    if (docs.length === 0) {
        return { status: 'missing_source' };
    }

    const operations = buildPixivImageMirrorOperations(docs);
    await state.collection.bulkWrite(operations, { ordered: false });
    return { status: 'synced', count: docs.length };
}
