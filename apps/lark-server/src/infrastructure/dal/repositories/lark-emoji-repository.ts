import { In, Repository } from 'typeorm';
import AppDataSource from '@ormconfig';
import { LarkEmoji } from '@entities/lark-emoji';

export class LarkEmojiRepository {
    private repository: Repository<LarkEmoji>;

    constructor() {
        this.repository = AppDataSource.getRepository(LarkEmoji);
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
    async upsertEmojis(emojis: Partial<LarkEmoji>[]): Promise<LarkEmoji[]> {
        return this.repository.save(emojis);
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
