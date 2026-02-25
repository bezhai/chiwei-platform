import { context } from '@middleware/context';
import { multiBotManager } from './multi-bot-manager';

function getBotConfigInternal() {
    const botName = context.getBotName();

    if (!botName) {
        throw new Error('Bot name is not set in the context');
    }
    const botConfig = multiBotManager.getBotConfig(botName);
    if (botConfig) {
        return botConfig;
    }
    throw new Error(`Bot configuration not found for bot: ${botName}`);
}

export function getBotUnionId(): string {
    return getBotConfigInternal().robot_union_id;
}

export function getBotAppId(): string {
    return getBotConfigInternal().app_id;
}
