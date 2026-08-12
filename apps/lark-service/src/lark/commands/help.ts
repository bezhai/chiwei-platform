// 「帮助」：把飞书后台那张卡片模板挂回触发的这条消息上。
//
// 本地没有卡片的内容，只有它的 id —— 卡片本身在飞书开放平台的搭建工具里，改文案不
// 用发版。所以这条指令的全部实现就是"用哪个模板、回哪条消息"两件事。
//
// ## 谓词与拆分前逐字相同
//
//   `EqualText('帮助')`   整句相等，不是包含 —— 「怎么用帮助啊」不该弹卡片
//   `TextMessageLimit`    只认纯文本消息
//   `NeedRobotMention`    群里必须 @ 到我（私聊直通）
//
// **没有 `OnlyGroup`**：私聊直接敲「帮助」是要给答案的。
//
// ## 这一句 await，拆分前不 await
//
// 拆分前是 fire-and-forget，飞书拒收这张卡片时留下的是一个没人接的 rejection ——
// 而本进程的 unhandledRejection 处理器是 `process.exit(1)`，一次回复失败会把持着飞书
// 长连的进程带走。await 之后失败收敛成引擎的 handler_error（有日志、不杀进程）。与
// ../repeat/toggle.ts 同一个理由、同一个处理。

import { EqualText, NeedRobotMention, TextMessageLimit } from '@inner/shared/rules';

import type { LarkCommand, LarkCommandDeps } from '../rules/commands';

/**
 * 飞书后台那张帮助卡片的模板 id。
 *
 * **字面量本身是线上配置的一部分**，跟文案一样不能顺手改：这里写错一个字符，飞书只会
 * 拒收这条 reply，用户看到的是「敲了帮助没反应」，日志里也只有一条 API 报错。
 */
export const HELP_CARD_TEMPLATE = 'ctp_AAYrltZoypBP';

export function helpCommand(deps: LarkCommandDeps): LarkCommand {
    return (context) => ({
        rules: [EqualText('帮助'), TextMessageLimit, NeedRobotMention],
        comment: '给用户发送帮助信息',
        category: 'utility',
        handler: async () => {
            // 第三个参数是模板变量。这张卡片没有变量，如实传 undefined —— 传一个空对象
            // 会让"这张卡片不吃变量"和"这次没有变量可传"看起来是同一件事。
            await deps.api.replyTemplate(context.message.messageId, HELP_CARD_TEMPLATE, undefined);
        },
    });
}
