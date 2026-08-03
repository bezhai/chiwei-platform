// 进程内的 bot 身份目录：本服务负责哪些 bot、每个 bot 在 common_user 里是谁。
//
// 目录本身渠道无关 —— 它只知道 bot_config 有一个 channel 字符串列，不知道具体
// 取值是什么。但**按 channel 过滤加载**是拆服务的硬约束：一个服务不该持有别的
// 渠道的 credentials。过滤下推到查询条件（不属于本服务的行压根不取回来），而不
// 是取回全表再在内存里挑 —— 后者等于凭据已经进了进程，只是没被读而已。

import { In } from 'typeorm';
import { v7 as uuidv7 } from 'uuid';

import { BotConfig } from '../entities/bot-config';
import { CommonUser } from '../entities/common-user';
import { repositoryFor } from '../persistence/data-source';

export interface BotLoadOptions {
    // 本服务负责的 channel 清单。不传 = 不加 channel 条件（全渠道加载）。
    // 传空数组 fail-closed：那既不是"全部"也不是"某几个"，只会静默加载零个
    // bot，服务起来了但一条消息都不认 —— 必须在启动期炸。
    channels?: string[];
}

// 范围的规范形式，用来判断两次 load 要的是不是同一批 bot。
// undefined（不过滤）与任何具体清单都不同；清单只看集合、不看顺序。
function scopeKey(channels: string[] | undefined): string {
    if (channels === undefined) return '*';
    return [...new Set(channels)].sort().join(',');
}

function describeScope(channels: string[] | undefined): string {
    return channels === undefined ? 'all channels' : `channels=[${channels.join(',')}]`;
}

export class BotDirectory {
    private botConfigs = new Map<string, BotConfig>();
    private loadedScope: { key: string; description: string } | null = null;

    /**
     * 从 bot_config 加载本服务负责的 bot，并确保每个 bot 在 common_user 里有身份。
     *
     * 同一范围重复调用幂等（启动链路里多处调用不会重复查库）。**不同范围重复调用
     * fail-loud**：幂等 + 范围收窄会架空按 channel 过滤的隔离 —— 先无参 load() 已经
     * 把所有渠道的 bot 连凭据读进了进程，之后 load({channels:[...]}) 若静默返回，
     * 调用方会以为自己拿到了隔离，实际别的渠道的凭据还在内存里。这种"看起来生效、
     * 实际没生效"的隔离比没有隔离更危险，必须在启动期炸出来。
     */
    async load(options: BotLoadOptions = {}): Promise<void> {
        const key = scopeKey(options.channels);
        if (this.loadedScope !== null) {
            if (this.loadedScope.key === key) return;
            throw new Error(
                `BotDirectory already loaded for ${this.loadedScope.description}; ` +
                    `refusing to reload for ${describeScope(options.channels)} — ` +
                    'bots already in memory (and their credentials) are not unloaded by a ' +
                    'narrower reload, so the second scope would be silently wrong. ' +
                    'Load once, at the composition root.',
            );
        }

        const where: Record<string, unknown> = { is_active: true };
        if (options.channels !== undefined) {
            if (options.channels.length === 0) {
                throw new Error(
                    'BotDirectory.load({ channels: [] }) loads zero bots; ' +
                        'omit channels to load every channel, or name the channels this service owns',
                );
            }
            where.channel = In(options.channels);
        }

        const bots = await repositoryFor(BotConfig).find({
            where,
            order: { bot_name: 'ASC' },
        });
        await this.ensureCommonUsers(bots);

        this.botConfigs.clear();
        for (const bot of bots) {
            this.botConfigs.set(bot.bot_name, bot);
        }
        this.loadedScope = { key, description: describeScope(options.channels) };
        console.info(
            `[bot-directory] loaded ${bots.length} bot(s)` +
                (options.channels ? ` for channels=[${options.channels.join(',')}]` : ''),
        );
    }

    getBotConfig(botName: string): BotConfig | null {
        return this.botConfigs.get(botName) ?? null;
    }

    getAllBotConfigs(): BotConfig[] {
        return Array.from(this.botConfigs.values());
    }

    /**
     * bot 在 common_user 里的身份。取不到直接抛 —— 下游拿它当 common_user_id
     * 往 common_message 里写，返回空串等于写脏数据。
     */
    getBotCommonUserId(botName: string): string {
        const bot = this.getBotConfig(botName);
        if (!bot) {
            throw new Error(`bot configuration not found for bot: ${botName}`);
        }
        if (!bot.common_user_id) {
            throw new Error(
                `bot ${botName} has no common_user_id; bot identity initialization ` +
                    `must run before channel runtime starts`,
            );
        }
        return bot.common_user_id;
    }

    /** 按入站接入方式取 bot；onlyCurrentEnv=true 时再按 IS_DEV 匹配 is_dev。 */
    getBotsByInitType(initType: 'http' | 'websocket', onlyCurrentEnv = false): BotConfig[] {
        const isDevEnv = process.env.IS_DEV === 'true';
        return this.getAllBotConfigs().filter((bot) => {
            if (bot.init_type !== initType) return false;
            if (!onlyCurrentEnv) return true;
            return bot.is_dev === isDevEnv;
        });
    }

    private async ensureCommonUsers(bots: BotConfig[]): Promise<void> {
        const commonUserRepo = repositoryFor(CommonUser);
        const botRepo = repositoryFor(BotConfig);

        for (const bot of bots) {
            const commonUserId = bot.common_user_id ?? uuidv7();
            await commonUserRepo.upsert(
                {
                    common_user_id: commonUserId,
                    channel: bot.channel,
                    display_name: bot.bot_name,
                },
                ['common_user_id'],
            );

            if (!bot.common_user_id) {
                await botRepo.update({ bot_name: bot.bot_name }, { common_user_id: commonUserId });
                bot.common_user_id = commonUserId;
                console.info(
                    `[bot-directory] assigned common_user_id=${commonUserId} to bot=${bot.bot_name}`,
                );
            }
        }
    }
}

// 进程级单例：组装根在启动期 load()，其余模块只读。与 channel-registry /
// command-registry 的单例取向一致。
export const botDirectory = new BotDirectory();
