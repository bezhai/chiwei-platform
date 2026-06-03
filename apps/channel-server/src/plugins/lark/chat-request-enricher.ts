import type { RuleMessage } from '@core/rules/rule-message';
import type { ChatRequestEnrichment } from '@core/services/ai/reply';
import { larkContextStore } from './lark-context-store';
import { getLarkBotConfigByCommonUserId } from './bot-identity';

// 飞书侧 chat.request 富化。mention 里的已注册 bot 在入站投影阶段已经换成
// common_user_id，这里只把 common bot identity 收敛成 persona_id。
export function enrichLarkChatRequest(message: RuleMessage): ChatRequestEnrichment {
    if (message.channel !== 'lark') {
        return { isCanary: false, personaIds: [] };
    }
    const lark = larkContextStore.get(message);
    const personaIds = message.mentionedUserIds
        .map((commonUserId) => getLarkBotConfigByCommonUserId(commonUserId)?.persona_id)
        .filter((personaId): personaId is string => Boolean(personaId));
    return {
        isCanary: lark.basicChatInfo?.permission_config?.is_canary ?? false,
        personaIds: Array.from(new Set(personaIds)),
    };
}
