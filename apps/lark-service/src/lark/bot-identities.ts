import type { DataSource } from 'typeorm';
import { BotConfig, CommonUser } from '@inner/shared/entities';
import { LARK_CHANNEL } from './channel';

/** 身份目录包含停用配置；客户端启用范围仍由 BotDirectory 决定。 */
export async function loadLarkIdentityBots(source: DataSource): Promise<BotConfig[]> {
    return source.transaction(async manager => {
        // 多个入站部署可同时启动。锁住配置行后分配身份，避免各自生成一个 ID。
        const repository = manager.getRepository(BotConfig);
        const bots = await repository.createQueryBuilder('bot')
            .where('bot.channel = :channel', { channel: LARK_CHANNEL })
            .orderBy('bot.bot_name', 'ASC')
            .setLock('pessimistic_write')
            .getMany();
        for (const bot of bots) {
            const id = bot.common_user_id ?? Bun.randomUUIDv7();
            await manager.getRepository(CommonUser).upsert({
                common_user_id: id, channel: LARK_CHANNEL, display_name: bot.bot_name,
            }, ['common_user_id']);
            if (!bot.common_user_id) {
                await repository.update({ bot_name: bot.bot_name }, { common_user_id: id });
                bot.common_user_id = id;
            }
        }
        return bots;
    });
}
