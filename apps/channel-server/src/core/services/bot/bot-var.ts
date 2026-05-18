import { context } from '@middleware/context';
import { multiBotManager } from './multi-bot-manager';
import { larkCredentials } from './lark-credentials';

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

// 签名与返回类型保持不变，调用方零感知。内部从 credentials JSONB 取（旧独立
// 列 app_id / robot_union_id 已删）；对同一 bot 返回值与改造前完全一致。
export function getBotAppId(): string {
    return larkCredentials(getBotConfigInternal()).app_id;
}

export function getBotUnionId(): string {
    return larkCredentials(getBotConfigInternal()).robot_union_id;
}
