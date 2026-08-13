// `/bind` `/unbind`：把一个人绑在这个群上，他退群就自动拉回来。
//
//     /bind @张三
//        └─▶ @ 到人了吗 ─▶ 飞书认识他吗 ─▶ 他在这个群里吗 ─▶ 已经绑过了吗 ─▶ 绑
//
// 四道闸每一道都有自己那句话，逐字照搬 —— 它们是管理员唯一能看到的诊断信息。
//
// ## 这两条**不判管理员**，与拆分前一致
//
// 同一组里只有 `/block` 一族判。看着像疏漏，但改它就是改线上的准入范围，不属于等价迁移。
//
// ## `/unbind` 是软删
//
// 行留着、只把 `is_active` 关掉（见 ../../entities/user-group-binding.ts）。所以判"已经
// 绑过了吗"必须连 `is_active` 一起看：把解绑过的行当成"已经绑过"，管理员会看到"无需
// 重复绑定"而退群时没人拉他。
//
// ## 飞书那次查人只看它**抛不抛**
//
// 上游 `try { await getUserInfo(...) } catch { 回一句 e.message }`，**不看返回值**。所以
// "查不到这个人"（端口交回 null）不在这里拦，而是落到下一道"他在这个群里吗"闸上，
// 说的是「该用户不在群中，无法绑定」。抛出来的那种是另一回事（多半是应用没有通讯录
// 权限），把飞书的原话转达给管理员。照搬。

import { firstMentionedHuman } from '../message/mentions';
import type { LarkCommandDeps, LarkSlashCommand } from '../rules/commands';

const BOUND = '绑定成功，该用户退群后将被自动重新拉回群聊';
const UNBOUND = '解绑成功，该用户退群后将不会被自动拉回群聊';

export function bindCommand(deps: LarkCommandDeps): LarkSlashCommand {
    return async (_message, context) => {
        const say = (text: string) => deps.api.replyText(context.message.messageId, text, true);
        const who = firstMentionedHuman(context.mentions);
        if (!who) return void (await say('请@具体用户进行绑定'));

        try {
            await deps.api.getUser(who);
        } catch (error) {
            // 飞书的原话原样转达。多半是"应用没有通讯录权限"这类，管理员看得懂。
            return void (await say(error instanceof Error ? error.message : '未知错误'));
        }

        const chatId = context.message.chatId;
        // 上游那次查询带着 `is_leave: false`，所以退了群的人在这里就是"不在群中"。
        const member = await deps.store.larkGroupMember(chatId, who);
        if (member?.is_leave !== false) return void (await say('该用户不在群中，无法绑定'));

        const binding = await deps.store.larkGroupBinding(chatId, who);
        if (binding?.is_active) return void (await say('该用户已绑定，无需重复绑定'));

        if (binding) {
            // 解绑过的行复用，不再插一条 —— 那张表上没有唯一约束，插第二条就多一行。
            await deps.store.setLarkGroupBindingActive(chatId, who, true);
        } else {
            await deps.store.insertLarkGroupBinding(chatId, who);
        }
        await say(BOUND);
    };
}

export function unbindCommand(deps: LarkCommandDeps): LarkSlashCommand {
    return async (_message, context) => {
        const say = (text: string) => deps.api.replyText(context.message.messageId, text, true);
        const who = firstMentionedHuman(context.mentions);
        if (!who) return void (await say('请@具体用户进行解绑'));

        const chatId = context.message.chatId;
        const binding = await deps.store.larkGroupBinding(chatId, who);
        if (!binding?.is_active) return void (await say('该用户未绑定，无需解绑'));

        await deps.store.setLarkGroupBindingActive(chatId, who, false);
        await say(UNBOUND);
    };
}
