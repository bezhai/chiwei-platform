import type { BotConfig } from '@entities/bot-config';

export function larkIngressBots(bots: BotConfig[]): BotConfig[] {
    return bots.filter((bot) => bot.channel === 'lark');
}
