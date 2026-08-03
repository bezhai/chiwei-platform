// chat.request 上那两个只有飞书答得出来的字段。
//
// 其余 11 个字段由共享包按 RuleMessage 直接派生（见 @inner/shared/rules 的
// buildChatRequestPayload）—— 那些是所有渠道共通的口径，飞书没有发言权。

import type { ChatRequestEnricher, ChatRequestEnrichment } from '@inner/shared/rules';

/** 这个公共层用户是我们哪个 bot 的人设？不是我们的 bot 就没有。 */
export type LarkPersonaIdOf = (commonUserId: string) => string | undefined;

/**
 * persona_ids：**群聊唯一的应答者来源**。
 *
 * 语义是"被 @ 的人里，哪些是我们注册过的 bot，取它们的人设"。agent-service 那侧群聊
 * 靠它决定谁开口，空数组就是不回复 —— 所以这里错一点，症状就是赤尾在群里装死。
 *
 * 被 @ 的人在投影阶段就已经换成了 common_user_id（自家 bot 用目录里那个，真人现铸），
 * 所以这里只剩最后一跳：common_user_id → persona_id，再按人设去重（两个 bot 挂同一个
 * 人设时，那个人设只该被叫醒一次）。
 *
 * is_canary 恒 false。**这不是偷懒，是这个字段已经没有去处**：拆分前它读的是
 * lark_base_chat_info.permission_config.is_canary，但 agent-service 的 ChatTrigger 上
 * 根本没有这个字段，MQ source 在反序列化之前按 model_fields 过滤，它在那一步被静默
 * 丢掉（Python 侧全仓零引用）。给真值和给 false 在可观测行为上完全等价，为它把
 * permission_config 拉进投影端口不值得。**前提是"下游没有这个字段"** —— 哪天
 * ChatTrigger 真加上了它，要改的是这里加一个读 permission_config 的端口，不是别处。
 */
export function larkChatRequestEnricher(personaIdOf: LarkPersonaIdOf): ChatRequestEnricher {
    return (message): ChatRequestEnrichment => {
        const personaIds: string[] = [];
        for (const commonUserId of message.mentionedUserIds) {
            const personaId = personaIdOf(commonUserId);
            if (personaId && !personaIds.includes(personaId)) personaIds.push(personaId);
        }
        return { isCanary: false, personaIds };
    };
}
