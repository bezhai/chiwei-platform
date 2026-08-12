// `/block` `/unblock` `/blocklist`：黑名单。
//
// ## 准入是 handler 里的一句 if，不是规则谓词
//
// 这三条（连同已经删掉的 `/config`）在 handler 开头各自判一次管理员，非管理员收到的是
// 一句「只有管理员可以…」。**这与「余额」那条不一样** —— 那条把 IsAdmin 写在 rules 里，
// 非管理员是不命中、消息继续往后走。两种口径都照搬，不要统一：
//
//   * 统一成谓词：非管理员敲 `/block` 会掉进人格聊天，赤尾开始闲聊；
//   * 统一成 handler 判断：普通人 @ 赤尾说「余额」会收到一句拒绝，而那句话今天不存在。
//
// 三条各自判，也不抽一个公共的 gate —— 拒绝的话各不相同（拉黑 / 解除拉黑 / 查看黑名单），
// 抽出来之后要么参数化那句话（没省下什么），要么统一措辞（就是改线上文案）。
//
// ## 这条链路今天是断的，照搬不修
//
// 写进去的是**飞书的 union_id**，而共享规则引擎的 `NotBlocked` 按 **common_user_id** 查
// 同一列（列名 union_id 是历史遗留）。于是 `/block` 拉黑的人照样能跟赤尾说话，而管理员
// 收到的是「拉黑成功」。与拆分前逐字一致，登记在案（见 slash-tables.ts 的文件头）。

import { firstMentionedHuman } from '../message/mentions';
import type { LarkCommandDeps, LarkSlashCommand } from '../rules/commands';
import { postgresBlocklist } from './slash-tables';

export function blockCommand(deps: LarkCommandDeps): LarkSlashCommand {
    const blocklist = postgresBlocklist(deps.database);

    return async (_message, context) => {
        const say = (text: string) => deps.api.replyText(context.message.messageId, text, true);
        if (!context.isAdmin) return void (await say('只有管理员可以拉黑用户'));

        const who = firstMentionedHuman(context.mentions);
        if (!who) return void (await say('请@具体用户进行拉黑'));

        if (await blocklist.isBlocked(who)) return void (await say('该用户已在黑名单中'));

        // 记下是谁拉的。飞书没给发送者 union_id 时上游记的是 'unknown_sender'，照搬 ——
        // 那个字面量已经在库里了，换一个会让历史记录出现两种"不知道是谁"。
        await blocklist.block(who, context.message.sender.unionId ?? 'unknown_sender');
        await say('拉黑成功');
    };
}

export function unblockCommand(deps: LarkCommandDeps): LarkSlashCommand {
    const blocklist = postgresBlocklist(deps.database);

    return async (_message, context) => {
        const say = (text: string) => deps.api.replyText(context.message.messageId, text, true);
        if (!context.isAdmin) return void (await say('只有管理员可以解除拉黑'));

        const who = firstMentionedHuman(context.mentions);
        if (!who) return void (await say('请@具体用户进行解除拉黑'));

        if (!(await blocklist.isBlocked(who))) return void (await say('该用户不在黑名单中'));

        await blocklist.unblock(who);
        await say('解除拉黑成功');
    };
}

export function blocklistCommand(deps: LarkCommandDeps): LarkSlashCommand {
    const blocklist = postgresBlocklist(deps.database);

    return async (_message, context) => {
        const say = (text: string) => deps.api.replyText(context.message.messageId, text, true);
        if (!context.isAdmin) return void (await say('只有管理员可以查看黑名单'));

        const everyone = await blocklist.everyone();
        if (everyone.length === 0) return void (await say('黑名单为空'));

        const lines = everyone.map((unionId, at) => `${at + 1}. ${unionId}`).join('\n');
        await say(`黑名单列表:\n${lines}`);
    };
}
