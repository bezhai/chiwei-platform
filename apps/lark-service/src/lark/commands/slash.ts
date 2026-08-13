// 「指令处理」：清单里的一格，背后是九条 `/xxx` 子指令。
//
//     @bot /bind @张三
//        │
//        ├─ 谓词：clearText 以任何一个已知 key 开头吗
//        └─ handler：**所有**前缀命中的子指令挨个跑
//
// ## "所有命中的都跑"，不是"跑第一个"
//
// 匹配是 `^/{key}`，而 key 之间有前缀关系：`/blocklist` 同时命中 `block` 和 `blocklist`。
// 上游那个循环没有 break，于是两条都跑 —— 管理员敲 `/blocklist` 会先收到一句
// 「请@具体用户进行拉黑」，再收到名单。这是既有行为，照搬（改成"跑第一个"是在改可观测
// 行为，而且顺序一变哪条先跑又是另一个决定）。
//
// ## `/config` 不在分发表里，所以它现在什么都不匹配
//
// 拍板删掉（spec 已知缺陷四）。后果是敲 `/config …` 不再被这条规则接住，落进人格聊天 ——
// 与"敲一句赤尾不认识的话"一样。这是删掉一条指令唯一可观测的变化。
//
// ## 分发表来自清单本身
//
// `larkSlashDispatch` 直接拿 LARK_SLASH_COMMANDS 编表（见 ../rules/commands.ts），所以
// "清单里有、本体没接上"是装配期一声炸，不是线上才发现的静默失配。

import { NeedRobotMention, TextMessageLimit } from '@inner/shared/rules';
import type { RuleMessage } from '@inner/shared/rules';

import type { LarkCommand, LarkCommandDeps } from '../rules/commands';
import { larkSlashDispatch } from '../rules/commands';

/**
 * 这句话是不是以 `/{key}` 开头。
 *
 * key 进正则之前不转义，与拆分前一致 —— 今天九个 key 全是 `[a-z_]`，都不是正则元字符。
 * 造不出正则时当作不匹配（上游那个 try/catch 的意思）。
 */
export function matchesSlashKey(text: string, key: string): boolean {
    try {
        return new RegExp(`^/${key}`).test(text);
    } catch {
        return false;
    }
}

/** 这一格的本体：装配期编好分发表，之后每条消息按前缀挑子指令。 */
export function slashCommand(deps: LarkCommandDeps): LarkCommand {
    const table = larkSlashDispatch(deps);
    const keys = Object.keys(table);

    return (context) => ({
        rules: [
            (message: RuleMessage) =>
                keys.some((key) => matchesSlashKey(message.clearText(), key)),
            TextMessageLimit,
            NeedRobotMention,
        ],
        comment: '指令处理',
        category: 'utility',
        handler: async (message) => {
            const text = message.clearText();
            // 清单顺序即执行顺序，**不 break**：理由见文件头。
            //
            // **也不逐条兜异常**：一条抛出去之后剩下的不跑、整条收敛成引擎的
            // handler_error，与拆分前逐字一致。兜住它看起来更稳（`/blocklist` 那种一次
            // 跑两条的情况下前一条失败不影响后一条），但那是一个上游没有的行为 ——
            // 这批的验收口径只有"行为一致"这一条，边搬边改会让它失效。
            for (const key of keys) {
                if (matchesSlashKey(text, key)) {
                    await table[key]!(message, context);
                }
            }
        },
    });
}
