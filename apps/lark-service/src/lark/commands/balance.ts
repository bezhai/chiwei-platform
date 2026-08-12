// 「余额」：管理员问一句，赤尾把 302.ai 的账户情况贴成一张卡片。
//
//     @bot 余额 ──▶ 同时问余额和每个 key 的用量 ──▶ 拼卡片 ──▶ 挂在原消息上回复
//                                     └── 任一失败 ──▶ 「获取余额信息失败」
//
// ## 准入写在谓词里，不是 handler 里的一句 if
//
// 谓词与拆分前逐字相同：
//
//   `EqualText('余额')`   整句相等
//   `TextMessageLimit`    只认纯文本
//   `NeedRobotMention`    群里必须 @ 到我
//   `IsAdmin`             发送者是超级管理员
//
// 最后一条的位置是有后果的：写在 rules 里，非管理员敲「余额」是**不命中**（这条消息继续
// 往后试别的规则、最后由人格聊天接住）；挪进 handler 就变成"命中之后被拒绝"，终态从
// no_match 变成 responded —— @ 赤尾说「余额」的普通人从此得不到任何回应。
//
// 判据来自逐消息的指令上下文（投影读发送者档案时顺路带回来的 is_admin），这一层不再查库。
// 注意斜杠子指令那一组用的是**另一种口径**（handler 内联判断 + 一句"只有管理员可以…"），
// 两种都照搬，不要统一 —— 它们对非管理员的可观测行为不一样。
//
// ## 金额那四列的 0 是"没有上限"，不是"上限是零"
//
// 所以 0 显示成 `-` 而不是 `0.000`。逐字照搬上游的 costFormat。

import {
    CardHeader,
    LarkCard,
    MarkdownComponent,
    TableColumn,
    TableComponent,
} from 'feishu-card';
import { EqualText, NeedRobotMention, TextMessageLimit } from '@inner/shared/rules';

import type { LarkCommandContext } from '../rules/command-context';
import type { LarkCommand, LarkCommandDeps } from '../rules/commands';
import type { LarkAiKeyUsage } from './ai-provider';

/** 查不到时对着管理员说的那一句。逐字照搬。 */
const LOOKUP_FAILED = '获取余额信息失败';

/** 卡片那张表的一行。列名与 302.ai 的字段名一致，所以表头和数据不需要翻译层。 */
interface UsageRow {
    api_name: string;
    limit_daily_cost: string;
    current_date_cost: string;
    limit_cost: string;
    current_cost: string;
}

/** 千分之一单位 → 三位小数；**0 是"没有上限"，显示成 `-`**。 */
function cost(value: number): string {
    return value > 0 ? (value / 1000).toFixed(3) : '-';
}

function usageRow(key: LarkAiKeyUsage): UsageRow {
    return {
        api_name: key.api_name,
        limit_daily_cost: cost(key.limit_daily_cost),
        current_date_cost: cost(key.current_date_cost),
        limit_cost: cost(key.limit_cost),
        current_cost: cost(key.current_cost),
    };
}

export function balanceCard(balance: string, keys: LarkAiKeyUsage[]): LarkCard {
    return new LarkCard()
        .withHeader(new CardHeader('302AI使用情况').color('orange'))
        .addElement(
            new MarkdownComponent(`**当前余额：** ${balance}`),
            new TableComponent<UsageRow>()
                .addColumn(new TableColumn('api_name').setDisplayName('API名称'))
                .addColumn(new TableColumn('limit_daily_cost').setDisplayName('每日上限'))
                .addColumn(new TableColumn('current_date_cost').setDisplayName('今日消耗'))
                .addColumn(new TableColumn('limit_cost').setDisplayName('消耗总上限'))
                .addColumn(new TableColumn('current_cost').setDisplayName('当前总消耗'))
                .appendRows(...keys.map(usageRow)),
        );
}

export function balanceCommand(deps: LarkCommandDeps): LarkCommand {
    return (context) => ({
        rules: [
            EqualText('余额'),
            TextMessageLimit,
            NeedRobotMention,
            // IsAdmin。判据来自投影顺路读到的那一行，不再查库。
            () => context.isAdmin,
        ],
        comment: '发送余额信息',
        category: 'utility',
        handler: async () => {
            try {
                // 两个端点互不依赖，一起问。
                const [balance, keys] = await Promise.all([
                    deps.aiProvider.balance(),
                    deps.aiProvider.apiKeys(),
                ]);
                // 拆分前 replyCard 没传 replyInThread，即不进话题。
                await deps.api.replyCard(
                    context.message.messageId,
                    balanceCard(balance, keys),
                    false,
                );
            } catch (error) {
                await apologise(deps, context, error);
            }
        },
    });
}

/**
 * 失败只说一句固定的话（不把 302.ai 的报错贴给用户）。**自己再失败也不外溢** —— 与
 * ../photo/send-photo.ts 的 apologise 同一个处理。那一句**进话题**，照搬。
 */
async function apologise(
    deps: LarkCommandDeps,
    context: LarkCommandContext,
    error: unknown,
): Promise<void> {
    console.error('[lark-balance] 余额 lookup failed:', error);
    try {
        await deps.api.replyText(context.message.messageId, LOOKUP_FAILED, true);
    } catch (replyError) {
        console.error('[lark-balance] could not even tell the user 余额 failed:', replyError);
    }
}
