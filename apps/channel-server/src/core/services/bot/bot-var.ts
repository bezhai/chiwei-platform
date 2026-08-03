import { context } from '@middleware/context';
import { botDirectory } from '@inner/shared/bot';

function getBotConfigInternal() {
    const botName = context.getBotName();

    if (!botName) {
        throw new Error('Bot name is not set in the context');
    }
    const botConfig = botDirectory.getBotConfig(botName);
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
