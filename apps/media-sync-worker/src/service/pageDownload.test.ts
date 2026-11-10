import { describe, expect, it } from 'bun:test';
import type { MultiTag, UploadImgV2Req } from '../mongo/types';
import { downloadPage, type PageDownloadDeps } from './pageDownload';

const KEY = 'pixiv_img_v2/20261110/123_p0.png';
const TAGS: MultiTag[] = [{ name: '猫', translation: 'cat', visible: true }];
const IMAGE = { need_download: true, author: 'artist', author_id: '42', is_r18: false, title: 'title' };

function recordingDeps(overrides: Partial<PageDownloadDeps> = {}) {
    const steps: string[] = [];
    const recorded: UploadImgV2Req[] = [];
    const deps: PageDownloadDeps = {
        checkExistPixivImg: async () => {
            steps.push('check');
            return false;
        },
        waitBeforeDownload: async () => {},
        downloadToOss: async () => {
            steps.push('download');
            return KEY;
        },
        addImage: async (_tags, image) => {
            steps.push('record');
            recorded.push(image);
        },
        runPostDownloadSync: async () => {
            steps.push('post_sync');
        },
        ...overrides,
    };
    return { deps, steps, recorded };
}

const IMAGE_URL = 'https://i.pximg.net/img-original/img/2026/11/10/00/00/00/123_p0.png';

function request(imageUrl: string | undefined) {
    return { illustId: '123', pageIndex: 0, imageUrl, tags: TAGS, image: IMAGE };
}

describe('downloadPage', () => {
    it('writes the catalog record with the key the server returned, before post-download sync', async () => {
        const { deps, steps, recorded } = recordingDeps();

        const metrics = await downloadPage(request(IMAGE_URL), deps);

        expect(metrics.status).toBe('downloaded');
        expect(steps).toEqual(['check', 'download', 'record', 'post_sync']);
        expect(recorded).toEqual([{ pixiv_name: '123_p0.png', tos_file_name: KEY, ...IMAGE }]);
    });

    it('records nothing when the server download fails', async () => {
        const { deps, steps } = recordingDeps({
            downloadToOss: async () => {
                throw new Error('image-store download returned no tos_file_name');
            },
        });

        const metrics = await downloadPage(request(IMAGE_URL), deps);

        expect(metrics).toMatchObject({ status: 'download_failed', error: 'image-store download returned no tos_file_name' });
        expect(steps).toEqual(['check']);
    });

    it('does not hand off to tagger/MinIO when the catalog write fails', async () => {
        const { deps, steps } = recordingDeps({
            addImage: async () => {
                throw new Error('mongo down');
            },
        });

        const metrics = await downloadPage(request(IMAGE_URL), deps);

        expect(metrics).toMatchObject({ status: 'add_image_failed', error: 'mongo down' });
        expect(steps).toEqual(['check', 'download']);
    });

    it('replays post-download sync for a page already in the catalog without downloading it again', async () => {
        const { deps, steps } = recordingDeps({
            checkExistPixivImg: async () => {
                steps.push('check');
                return true;
            },
        });

        const metrics = await downloadPage(request(IMAGE_URL), deps);

        expect(metrics.status).toBe('exists');
        expect(steps).toEqual(['check', 'post_sync']);
    });

    it('fails a page without an image URL', async () => {
        const { deps, steps } = recordingDeps();

        const metrics = await downloadPage(request(undefined), deps);

        expect(metrics.status).toBe('missing_url');
        expect(steps).toEqual([]);
    });
});
