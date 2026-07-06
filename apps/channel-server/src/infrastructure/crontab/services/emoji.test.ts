import { beforeEach, describe, expect, it, mock } from 'bun:test';
import type { Repository } from 'typeorm';
import { EmojiService } from './emoji';
import { LarkEmojiRepository, type LarkEmojiRow } from '@repositories/lark-emoji-repository';
import type { LarkEmoji } from '@entities/lark-emoji';

const replaceAllEmojisMock = mock(async (_emojis: LarkEmojiRow[]) => undefined);
const getAllEmojisMock = mock(async () => []);
const getEmojiByKeyMock = mock(async (_key: string) => null);
const getEmojiByTextMock = mock(async (_texts: string[]) => []);

const serviceRepository = {
    replaceAllEmojis: replaceAllEmojisMock,
    getAllEmojis: getAllEmojisMock,
    getEmojiByKey: getEmojiByKeyMock,
    getEmojiByText: getEmojiByTextMock,
} as unknown as LarkEmojiRepository;

const upsertMock = mock(async () => undefined);
const deleteMock = mock(async () => undefined);
const saveMock = mock(async () => {
    throw new Error('save should not be used for lark_emoji sync');
});
const transactionalRepo = {
    upsert: upsertMock,
    delete: deleteMock,
    save: saveMock,
};
const transactionMock = mock(
    async (
        fn: (manager: { getRepository: () => typeof transactionalRepo }) => Promise<void>,
    ) => fn({ getRepository: () => transactionalRepo }),
);
const typeormRepository = {
    manager: {
        transaction: transactionMock,
    },
} as unknown as Repository<LarkEmoji>;

describe('EmojiService.syncEmojiData', () => {
    beforeEach(() => {
        replaceAllEmojisMock.mockClear();
    });

    it('replaces the emoji set without clearing the table first', async () => {
        const service = new EmojiService(serviceRepository);
        service.fetchEmojiData = mock(async () => ({
            emojiData: {
                active: {
                    key: 'OK',
                    text: 'ok',
                    imageKey: 'img_ok',
                    isDeleted: false,
                },
                deleted: {
                    key: 'DELETED',
                    text: 'deleted',
                    imageKey: 'img_deleted',
                    isDeleted: true,
                },
            },
        }));

        await service.syncEmojiData();

        expect(replaceAllEmojisMock).toHaveBeenCalledWith([{ key: 'OK', text: 'ok' }]);
    });
});

describe('LarkEmojiRepository.replaceAllEmojis', () => {
    beforeEach(() => {
        upsertMock.mockClear();
        deleteMock.mockClear();
        saveMock.mockClear();
        transactionMock.mockClear();
    });

    it('uses native upsert in one transaction so concurrent syncs do not hit primary-key conflicts', async () => {
        const repository = new LarkEmojiRepository(typeormRepository);

        await repository.replaceAllEmojis([
            { key: 'OK', text: 'ok' },
            { key: 'THUMBSUP', text: 'thumbsup' },
        ]);

        expect(transactionMock).toHaveBeenCalledTimes(1);
        expect(saveMock).not.toHaveBeenCalled();
        expect(upsertMock).toHaveBeenCalledWith(
            [
                { key: 'OK', text: 'ok' },
                { key: 'THUMBSUP', text: 'thumbsup' },
            ],
            {
                conflictPaths: ['key'],
                skipUpdateIfNoValuesChanged: true,
            },
        );
        expect(deleteMock).toHaveBeenCalledTimes(1);
    });
});
