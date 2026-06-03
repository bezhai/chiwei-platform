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

export function getBotCommonUserId(): string {
    const id = getBotConfigInternal().common_user_id;
    if (!id) {
        throw new Error(
            `Bot ${context.getBotName()} has no common_user_id; ` +
                'bot identity initialization must run before message handling',
        );
    }
    return id;
}
