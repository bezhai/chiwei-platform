import { In } from 'typeorm';
import { BotConfig } from '@entities/bot-config';
import { BotPersona } from '@entities/bot-persona';
import { botConfigRepository } from '@repositories/bot-config-repository';
import AppDataSource from 'ormconfig';

export class MultiBotManager {
    private static instance: MultiBotManager;
    private botConfigs: Map<string, BotConfig> = new Map();
    private appIdToDisplayName: Map<string, string> = new Map();
    private initialized = false;

    private constructor() {}

    static getInstance(): MultiBotManager {
        if (!MultiBotManager.instance) {
            MultiBotManager.instance = new MultiBotManager();
        }
        return MultiBotManager.instance;
    }

    // 初始化加载所有机器人配置
    async initialize(): Promise<void> {
        if (this.initialized) return;

        // 所有启用的机器人都加载进内存；后续由调用方按环境筛选是否启动 http/ws
        const allBots = await botConfigRepository.getAllActiveBots();
        this.botConfigs.clear();

        for (const bot of allBots) {
            this.botConfigs.set(bot.bot_name, bot);
        }

        // 预加载 app_id → persona display_name 映射
        await this.loadDisplayNames(allBots);

        this.initialized = true;
        console.info(`Loaded ${allBots.length} bot configurations`);
    }

    // 根据机器人名称获取配置
    getBotConfig(botName: string): BotConfig | null {
        return this.botConfigs.get(botName) || null;
    }

    // 根据app_id获取机器人配置
    getBotConfigByAppId(appId: string): BotConfig | null {
        for (const bot of this.botConfigs.values()) {
            if (bot.app_id === appId) {
                return bot;
            }
        }
        return null;
    }

    // 获取所有机器人配置
    getAllBotConfigs(): BotConfig[] {
        return Array.from(this.botConfigs.values());
    }

    // 获取指定初始化类型的机器人
    getBotsByInitType(initType: 'http' | 'websocket', onlyCurrentEnv = false): BotConfig[] {
        const isDevEnv = process.env.IS_DEV === 'true';
        return Array.from(this.botConfigs.values()).filter((bot) => {
            if (bot.init_type !== initType) return false;
            if (!onlyCurrentEnv) return true;
            return bot.is_dev === isDevEnv;
        });
    }

    // 根据 app_id 获取 persona display_name
    getDisplayNameByAppId(appId: string): string | null {
        return this.appIdToDisplayName.get(appId) || null;
    }

    private async loadDisplayNames(bots: BotConfig[]): Promise<void> {
        this.appIdToDisplayName.clear();
        const personaIds = bots
            .filter((b) => b.persona_id)
            .map((b) => b.persona_id!);

        if (personaIds.length === 0) return;

        const personaRepo = AppDataSource.getRepository(BotPersona);
        const personas = await personaRepo.findBy({
            persona_id: In(personaIds),
        });

        const personaMap = new Map(personas.map((p) => [p.persona_id, p.display_name]));

        for (const bot of bots) {
            if (bot.persona_id && personaMap.has(bot.persona_id)) {
                this.appIdToDisplayName.set(bot.app_id, personaMap.get(bot.persona_id)!);
            }
        }
    }

    // 重新加载配置
    async reload(): Promise<void> {
        this.initialized = false;
        await this.initialize();
    }
}

export const multiBotManager = MultiBotManager.getInstance();
