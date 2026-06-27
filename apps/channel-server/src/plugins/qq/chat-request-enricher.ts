import type { RuleMessage } from '@core/rules/rule-message';
import type { ChatRequestEnrichment } from '@core/services/ai/reply';
import { getQqBotConfigByCommonUserId } from './bot-identity';

// QQ 侧 chat.request 富化（对飞书 chat-request-enricher）。被 @ 的本 bot 在投影阶段
// 已折成 common_user_id（mentionedUserIds）；这里收敛成 persona_id。私聊无 mention →
// persona_ids=[]，与飞书私聊一致（agent-service 据 bot_name 兜底解析 persona）。
// QQ 本期无群灰度，isCanary 恒 false。
export function enrichQqChatRequest(message: RuleMessage): ChatRequestEnrichment {
    if (message.channel !== 'qq') {
        return { isCanary: false, personaIds: [] };
    }
    const personaIds = message.mentionedUserIds
        .map((commonUserId) => getQqBotConfigByCommonUserId(commonUserId)?.persona_id)
        .filter((personaId): personaId is string => Boolean(personaId));
    return {
        isCanary: false,
        personaIds: Array.from(new Set(personaIds)),
    };
}
