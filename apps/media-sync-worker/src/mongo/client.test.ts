import { describe, expect, it } from 'bun:test';
import { bindPixivCollections, ensurePixivIndexes, type PixivCollections } from './client';

// T3's import writes into these collections by name, so the names are a contract.
describe('bindPixivCollections', () => {
    it('binds the catalog, queue, translations and author throttle to chiwei_pixiv collections', () => {
        const service = { getCollection: (name: string) => ({ name }) };

        const collections = bindPixivCollections(service as any) as unknown as Record<string, { name: string }>;

        expect(Object.fromEntries(Object.entries(collections).map(([role, c]) => [role, c.name]))).toEqual({
            images: 'pixiv_images',
            downloadTasks: 'pixiv_download_tasks',
            tagTranslations: 'pixiv_tag_translations',
            authorDownloads: 'pixiv_author_downloads',
        });
    });
});

describe('ensurePixivIndexes', () => {
    it('creates the lookup indexes every hot query relies on', async () => {
        // Index key order matters to Mongo and toEqual ignores object key order,
        // so record each spec as an ordered list of [field, direction].
        const created: Array<{ collection: string; spec: Array<[string, unknown]>; name: unknown }> = [];
        const fake = (collection: string) => ({
            createIndex: async (spec: Record<string, unknown>, options: { name?: string }) => {
                created.push({ collection, spec: Object.entries(spec), name: options?.name });
                return options?.name ?? '';
            },
        });
        const collections = {
            images: fake('pixiv_images'),
            downloadTasks: fake('pixiv_download_tasks'),
            tagTranslations: fake('pixiv_tag_translations'),
            authorDownloads: fake('pixiv_author_downloads'),
        } as unknown as PixivCollections;

        await ensurePixivIndexes(collections);

        expect(created).toEqual([
            { collection: 'pixiv_images', spec: [['pixiv_addr', 1]], name: 'idx_pixiv_images_pixiv_addr' },
            { collection: 'pixiv_images', spec: [['illust_id', -1]], name: 'idx_pixiv_images_illust_id_desc' },
            { collection: 'pixiv_images', spec: [['update_time', 1], ['_id', 1]], name: 'idx_pixiv_images_update_time_id' },
            { collection: 'pixiv_images', spec: [['tos_file_name', 1]], name: 'idx_pixiv_images_tos_file_name' },
            { collection: 'pixiv_images', spec: [['multi_tags.name', 1]], name: 'idx_pixiv_images_multi_tags_name' },
            { collection: 'pixiv_download_tasks', spec: [['illust_id', 1]], name: 'idx_pixiv_download_tasks_illust_id' },
            // Claim ($or: status in [Pending, Fail] | status=Running and last_run_time < t) and the
            // dead-letter sweep (status=Running, last_run_time < t, retry_time >= max) both filter by
            // status equality first, then a last_run_time range: status leads, last_run_time follows.
            {
                collection: 'pixiv_download_tasks',
                spec: [['status', 1], ['last_run_time', 1]],
                name: 'idx_pixiv_download_tasks_status_last_run_time',
            },
            { collection: 'pixiv_tag_translations', spec: [['origin', 1]], name: 'idx_pixiv_tag_translations_origin' },
        ]);
    });
});
