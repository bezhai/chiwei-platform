import { describe, expect, it, mock } from 'bun:test';
import { DownloadTask } from '../mongo/types';
import { claimNextTask, downloadIllust, type IllustDownloadDeps } from './consumeService';
import { EnumIllustType, type GetIllustInfoBody } from './types';

const FILTERS = { bannedUsers: ['1001'], skipWords: ['AI生成'] };

describe('claimNextTask', () => {
    // Claiming spends retry budget ($inc retry_time). If the lists were read after the
    // claim, a Dynamic Config outage would fail every task it touched and push the
    // whole queue to Dead within minutes.
    it('does not claim a task while the content filters cannot be read', async () => {
        const claim = mock(async () => DownloadTask.createTask('101'));

        const outcome = await claimNextTask({
            readFilters: async () => {
                throw new Error('Dynamic Config pixiv_ban_users is missing');
            },
            claim,
        });

        expect(outcome).toEqual({ status: 'filters_unavailable', error: 'Dynamic Config pixiv_ban_users is missing' });
        expect(claim).not.toHaveBeenCalled();
    });

    it('hands the claimed task over together with the filters it must be checked against', async () => {
        const task = DownloadTask.createTask('101');

        const outcome = await claimNextTask({ readFilters: async () => FILTERS, claim: async () => task });

        expect(outcome).toEqual({ status: 'claimed', task, filters: FILTERS });
    });

    it('reports an idle queue', async () => {
        const outcome = await claimNextTask({ readFilters: async () => FILTERS, claim: async () => null });

        expect(outcome).toEqual({ status: 'idle' });
    });
});

const NO_DELAY = {
    afterIllustInfoMs: 0,
    beforePageDownloadMs: 0,
    afterTaskMs: 0,
    afterAuthorMs: 0,
    limiterCooldownMs: 0,
};
const PAGE_URL = 'https://i.pximg.net/img-original/img/2026/11/10/00/00/00/101_p0.png';

function illust(overrides: Partial<GetIllustInfoBody> = {}): GetIllustInfoBody {
    return {
        userId: '2002',
        userName: 'artist',
        illustTitle: 'title',
        illustType: EnumIllustType.IllustTypeJpg,
        tags: {
            tags: [{ tag: '猫', locked: false, deletable: false, userId: '2002', userName: 'artist' }],
        },
        ...overrides,
    };
}

function illustDeps(info: GetIllustInfoBody) {
    const downloadToOss = mock(async (_url: string) => 'pixiv_img_v2/20261110/101_p0.png');
    const deps: IllustDownloadDeps = {
        getIllustInfo: async () => info,
        getIllustPageDetail: async () => [{ urls: { original: PAGE_URL } as any, width: 1, height: 1 }],
        searchAndAddTranslate: async () => '',
        checkExistPixivImg: async () => false,
        downloadToOss,
        addImage: async () => {},
        runPostDownloadSync: async () => {},
    };
    return { deps, downloadToOss };
}

describe('downloadIllust applies the Dynamic Config lists', () => {
    it('does not download an illust by a banned author', async () => {
        const { deps, downloadToOss } = illustDeps(illust({ userId: '1001' }));

        await downloadIllust('101', { bannedUsers: ['1001'], skipWords: [] }, NO_DELAY, deps);

        expect(downloadToOss).not.toHaveBeenCalled();
    });

    it('does not download an illust whose tag contains a skip word', async () => {
        const { deps, downloadToOss } = illustDeps(illust({
            tags: {
                tags: [
                    { tag: '猫', locked: false, deletable: false, userId: '2002', userName: 'artist' },
                    { tag: 'AI生成イラスト', locked: false, deletable: false, userId: '2002', userName: 'artist' },
                ],
            },
        }));

        await downloadIllust('101', { bannedUsers: [], skipWords: ['AI生成'] }, NO_DELAY, deps);

        expect(downloadToOss).not.toHaveBeenCalled();
    });

    it('downloads an illust that no list matches', async () => {
        const { deps, downloadToOss } = illustDeps(illust());

        await downloadIllust('101', { bannedUsers: ['1001'], skipWords: ['AI生成'] }, NO_DELAY, deps);

        expect(downloadToOss).toHaveBeenCalledTimes(1);
        expect(downloadToOss).toHaveBeenCalledWith(PAGE_URL);
    });
});
