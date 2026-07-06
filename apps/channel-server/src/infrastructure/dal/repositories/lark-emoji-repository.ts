import { In, Not } from 'typeorm';
import type { Repository } from 'typeorm';
import AppDataSource from '@ormconfig';
import { LarkEmoji } from '@entities/lark-emoji';

export type LarkEmojiRow = Pick<LarkEmoji, 'key' | 'text'>;

export class LarkEmojiRepository {
    private repository: Repository<LarkEmoji>;

    constructor(repository: Repository<LarkEmoji> = AppDataSource.getRepository(LarkEmoji)) {
        this.repository = repository;
    }

    // 获取所有emoji
    async getAllEmojis(): Promise<LarkEmoji[]> {
        return this.repository.find({
            order: { key: 'ASC' },
        });
    }

    async getEmojiByText(texts: string[]): Promise<LarkEmoji[]> {
        return this.repository.find({
            where: { text: In(texts) },
        });
    }

    // 根据key获取emoji
    async getEmojiByKey(key: string): Promise<LarkEmoji | null> {
        return this.repository.findOne({
            where: { key },
        });
    }

    // 批量插入或更新emoji数据
    async upsertEmojis(emojis: LarkEmojiRow[]): Promise<void> {
        await this.repository.upsert(emojis, {
            conflictPaths: ['key'],
            skipUpdateIfNoValuesChanged: true,
        });
    }

    // 用远端有效集合替换本地集合；upsert 避免并发同步时主键冲突。
    async replaceAllEmojis(emojis: LarkEmojiRow[]): Promise<void> {
        if (emojis.length === 0) {
            return;
        }

        const keys = emojis.map((emoji) => emoji.key);

        await this.repository.manager.transaction(async (manager) => {
            const repository = manager.getRepository(LarkEmoji);
            await repository.upsert(emojis, {
                conflictPaths: ['key'],
                skipUpdateIfNoValuesChanged: true,
            });
            await repository.delete({ key: Not(In(keys)) });
        });
    }

    // 删除所有emoji（用于重新同步）
    async clearAllEmojis(): Promise<void> {
        await this.repository.clear();
    }

    // 根据keys批量删除emoji
    async deleteEmojisByKeys(keys: string[]): Promise<void> {
        await this.repository.delete(keys);
    }
}

export const larkEmojiRepository = new LarkEmojiRepository();
