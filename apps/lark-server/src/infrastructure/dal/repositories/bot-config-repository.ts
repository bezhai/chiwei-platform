import { Repository } from 'typeorm';
import AppDataSource from '@ormconfig';
import { BotConfig } from '@entities/bot-config';

export class BotConfigRepository {
    private repository: Repository<BotConfig>;

    constructor() {
        this.repository = AppDataSource.getRepository(BotConfig);
    }

    // 获取所有启用的机器人配置
    // - isDev 为 undefined：返回所有启用机器人
    // - isDev 为 boolean：按环境过滤
    async getAllActiveBots(isDev?: boolean): Promise<BotConfig[]> {
        const where: Record<string, any> = { is_active: true };
        if (typeof isDev === 'boolean') {
            where.is_dev = isDev;
        }

        return this.repository.find({
            where,
            order: { bot_name: 'ASC' },
        });
    }

    // 根据机器人名称获取配置
    async getBotByName(botName: string): Promise<BotConfig | null> {
        return this.repository.findOne({
            where: { bot_name: botName, is_active: true },
        });
    }

    // 根据app_id获取机器人配置
    async getBotByAppId(appId: string): Promise<BotConfig | null> {
        return this.repository.findOne({
            where: { app_id: appId, is_active: true },
        });
    }

    // 获取指定初始化类型的机器人
    async getBotsByInitType(initType: 'http' | 'websocket'): Promise<BotConfig[]> {
        return this.repository.find({
            where: { init_type: initType, is_active: true },
            order: { bot_name: 'ASC' },
        });
    }

    // 创建或更新机器人配置
    async upsertBot(botConfig: Partial<BotConfig>): Promise<BotConfig> {
        return this.repository.save(botConfig);
    }
}

export const botConfigRepository = new BotConfigRepository();
