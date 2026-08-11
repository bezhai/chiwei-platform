// 飞书专属指令清单。规则序列的前半段，也是 Task D 的迁移账本 —— 每个槽位要么已经填上
// 本服务里的规则，要么记着还欠谁一条。
//
// ## 顺序是契约，不是排版
//
// 拼在这批指令后面的是人格聊天，而它的谓词只有 `NeedRobotMention` —— 一条 @ 赤尾的消息
// 它必然命中。所以指令必须先获得匹配机会，否则所有 @bot 的消息都会先落进聊天、指令永远
// 轮不到（channel-server 那份清单的头注释写的就是这个理由，照它来）。
//
// 清单**内部**的先后同样照抄 channel-server：`Meme` 的谓词只有 `NeedRobotMention` 加一条
// async 判定，本身就近似 catch-all，它排到那几条 `EqualText` 前面会把它们全吃掉。
//
// ## 空槽位不进规则序列
//
// 还没搬过来的槽位不产出任何 RuleConfig。今天这份清单全是空的，规则序列因而只有人格聊天
// 一条 —— 与拆分前一致，因为这些指令此刻仍然由 channel-server 在跑。填一个槽位就是把
// `pendingIn` 换成 `rule`：清单里改一行、加一个 import，指令自己那份实现在自己的文件里。
//
// ## `/config` 没有槽位，这是决定不是遗漏
//
// 它是「指令处理」那个斜杠指令组里的子指令，写进 `lark_base_chat_info.gray_config`，而
// agent-service 读的是 `common_conversation.attachment_policy` —— 这条链路本来就是断的
// （spec 已知缺陷四）。bezhai 2026-08-11 拍板连指令一起删掉、不迁，所以它只出现在
// DROPPED_SLASH_COMMANDS 里。整组的其余九条照迁。

import type { RuleConfig } from '@inner/shared/rules';

/**
 * 哪一批迁移任务负责把这个槽位填上。
 *
 * - `D2` 发图与卡片回调与图片日报
 * - `D3` emoji 与复读
 * - `D4` 其余指令
 *
 * D1（入站附件管线）不碰指令，所以不在这里。全部填满之后这个类型连同 `pendingIn` 那个
 * 分支一起删 —— 它是 Task D 期间的脚手架，不是长期结构。
 */
export type LarkCommandBatch = 'D2' | 'D3' | 'D4';

/**
 * 清单里的一格。`name` 是跨服务对账的键，取的是 channel-server 那份清单里同一条指令的
 * `comment`。
 */
export type LarkCommandSlot =
    /** 已经搬过来了：这就是它在本服务里的规则。 */
    | { name: string; rule: RuleConfig }
    /** 还欠着：记着谁负责填，不参与规则序列。 */
    | { name: string; pendingIn: LarkCommandBatch };

/** 飞书专属指令，先后即优先级。 */
export const LARK_COMMANDS: LarkCommandSlot[] = [
    { name: '复读功能', pendingIn: 'D3' },
    { name: '发送余额信息', pendingIn: 'D4' },
    { name: '给用户发送帮助信息', pendingIn: 'D4' },
    { name: '撤回消息', pendingIn: 'D4' },
    { name: '生成水群历史卡片', pendingIn: 'D4' },
    { name: '开启复读', pendingIn: 'D3' },
    { name: '关闭复读', pendingIn: 'D3' },
    { name: '指令处理', pendingIn: 'D4' },
    { name: '发送图片', pendingIn: 'D2' },
    { name: 'Meme', pendingIn: 'D4' },
];

/**
 * 「指令处理」那个槽位背后的斜杠指令组。这一格是一条规则、九个子指令，所以子指令另立
 * 一份清单 —— 否则"少搬了一条"在顶层清单上看不出来。
 */
export const LARK_SLASH_COMMANDS: readonly string[] = [
    'chat_id',
    'message_id',
    'bind',
    'unbind',
    'block',
    'unblock',
    'blocklist',
    'session',
    'union_id',
];

/** 拍板删掉、不迁的子指令。理由见文件头。 */
export const DROPPED_SLASH_COMMANDS: readonly string[] = ['config'];

/** 清单里已经填好的那些规则，保持清单里的先后。 */
export function larkCommandRules(slots: LarkCommandSlot[]): RuleConfig[] {
    return slots.flatMap((slot) => ('rule' in slot ? [slot.rule] : []));
}
