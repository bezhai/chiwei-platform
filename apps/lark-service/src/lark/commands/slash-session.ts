// `/session`：回复赤尾说过的某一句、敲 `/session`，把那次对话的 session_id 念出来。
//
//     被回复的 om_id ──▶ lark_message ──▶ common_message ──▶ response_id
//
// 排查用：拿到 session_id 就能在 Langfuse 里翻出那次对话的完整 trace。
//
// ## 五种说不出答案的情况各有各的话
//
//   没回复任何消息       没有起点
//   lark_message 查不到  那条消息不是本服务记过的（很久以前 / 别的 bot 发的）
//   common_message 查不到 台账缺了那一半（理论上不该发生，但真发生了要说得出来）
//   role 不是 assistant  回复的是真人说的话，那不叫 session
//   没有 response_id     主动发的消息（睡前那种），它不挂在任何一次台账上
//
// 文案逐字照搬，包括颜文字 —— 它们是线上历史。
//
// ## 这条**不判管理员**
//
// 与拆分前一致。session_id 本身不是秘密，能敲出它的人也拿不到 Langfuse。

import type { LarkCommandDeps, LarkSlashCommand } from '../rules/commands';
import { postgresAgentSessions } from './slash-tables';

const NO_PARENT = '人家找不到要查询的消息啦，请回复人家说的某条消息再试试～';
const UNKNOWN_MESSAGE = '唔...这条消息人家不认识，找不到记录呢 (´•ω•`)';
const NOT_MINE = '这条消息不是人家发的哦，要回复人家的消息才能查 session 呀～';
const NO_SESSION = '找不到对应的触发消息，session 不见了呢 (；´д｀)';

export function sessionCommand(deps: LarkCommandDeps): LarkSlashCommand {
    const sessions = postgresAgentSessions(deps.database);

    return async (_message, context) => {
        const say = (text: string) => deps.api.replyText(context.message.messageId, text, true);

        const parentId = context.message.parentId;
        if (!parentId) return void (await say(NO_PARENT));

        const larkMessage = await deps.store.larkMessage(parentId);
        if (!larkMessage) return void (await say(UNKNOWN_MESSAGE));

        const session = await sessions.sessionOf(larkMessage.common_message_id);
        // 两段查不到说的是**同一句话**，与拆分前一致：对敲指令的人来说"没记录"就是
        // 没记录，中间断在哪一层是我们的事。
        if (!session) return void (await say(UNKNOWN_MESSAGE));

        if (session.role !== 'assistant') return void (await say(NOT_MINE));
        if (!session.responseId) return void (await say(NO_SESSION));

        await say(`找到啦！session_id 是：\n${session.responseId}`);
    };
}
