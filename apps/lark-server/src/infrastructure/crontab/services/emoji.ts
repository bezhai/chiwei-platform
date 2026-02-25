import http from '@http/client';
import { LarkEmoji } from '@entities/lark-emoji';
import { larkEmojiRepository } from '@repositories/lark-emoji-repository';
import { Crontab, registerCrontabService } from '@crontab/decorators';

export interface EmojiDataResponse {
    emojiData: {
        [key: string]: {
            key: string;
            text: string;
            imageKey: string;
            isDeleted: boolean;
            skinKeys?: string[];
            width?: number;
            height?: number;
            easterEgg?: {
                animationKey: string;
                easterEggPlayThreshold: { [key: string]: number };
                animationType: number;
            };
        };
    };
}

export class EmojiService {
    private static readonly EMOJI_API_URL = 'https://ywh-emoji-bot.fn-boe.bytedance.net/api/emojis';

    /**
     * 从远程API获取emoji数据
     */
    async fetchEmojiData(): Promise<EmojiDataResponse> {
        try {
            const response = await http.get<EmojiDataResponse>(EmojiService.EMOJI_API_URL);
            return response.data;
        } catch (error) {
            console.error('Failed to fetch emoji data:', error);
            throw new Error('Failed to fetch emoji data from remote API');
        }
    }

    /**
     * 提取有效的emoji数据（isDeleted=false）
     */
    private extractValidEmojis(emojiDataResponse: EmojiDataResponse): Partial<LarkEmoji>[] {
        const validEmojis: Partial<LarkEmoji>[] = [];

        Object.values(emojiDataResponse.emojiData).forEach((emojiData) => {
            if (!emojiData.isDeleted) {
                validEmojis.push({
                    key: emojiData.key,
                    text: emojiData.text,
                });
            }
        });

        return validEmojis;
    }

    /**
     * 同步emoji数据到数据库
     * 每小时执行一次
     */
    @Crontab('0 * * * *', { taskName: 'emoji-sync', botName: 'bytedance' })
    async syncEmojiData(): Promise<void> {
        try {
            console.info('Starting emoji data sync...');

            // 1. 获取远程数据
            const emojiDataResponse = await this.fetchEmojiData();
            console.info(
                `Fetched ${Object.keys(emojiDataResponse.emojiData).length} emojis from API`,
            );

            // 2. 提取有效数据
            const validEmojis = this.extractValidEmojis(emojiDataResponse);
            console.info(`Found ${validEmojis.length} valid emojis (isDeleted=false)`);

            if (validEmojis.length === 0) {
                console.warn('No valid emojis found to sync');
                return;
            }

            // 3. 清空现有数据
            await larkEmojiRepository.clearAllEmojis();
            console.info('Cleared existing emoji data');

            // 4. 批量插入新数据
            await larkEmojiRepository.upsertEmojis(validEmojis);
            console.info(`Successfully synced ${validEmojis.length} emojis to database`);
        } catch (error) {
            console.error('Failed to sync emoji data:', error);
            throw error;
        }
    }

    /**
     * 获取所有emoji数据
     */
    async getAllEmojis(): Promise<LarkEmoji[]> {
        return larkEmojiRepository.getAllEmojis();
    }

    /**
     * 根据key获取emoji
     */
    async getEmojiByKey(key: string): Promise<LarkEmoji | null> {
        return larkEmojiRepository.getEmojiByKey(key);
    }

    /**
     * 根据name获取emoji
     */
    async getEmojiByText(texts: string[]): Promise<LarkEmoji[]> {
        return larkEmojiRepository.getEmojiByText(texts);
    }
}

export const emojiService = new EmojiService();

// 注册定时任务
registerCrontabService(emojiService);

