// 「开启复读」「关闭复读」：拨这个会话上的 `open_repeat_message` 开关。
//
// 两条指令除了开关的值和回的那句话之外一模一样，所以本文件是一个工厂造出两条 —— 与
// 拆分前的 `changeRepeatStatus(true|false)` 同形。
//
// ## 谓词与拆分前逐字相同
//
//   `EqualText('开启复读')`  整句相等，不是包含 —— "怎么开启复读啊" 不该真的开启
//   `TextMessageLimit`      只认纯文本消息
//   `NeedRobotMention`      群里必须 @ 到我
//   `OnlyGroup`             私聊里没有"群开关"这回事
//
// ## 写的是 permission_config 这团 jsonb，必须合并不能覆盖
//
// 同一列上还住着 `allow_send_pixiv_image`、`allow_send_limit_photo` 这些别的开关（见
// projection/tables.ts 的 LarkChatPermission）。整列覆写会把它们一起抹掉，而症状是
// "有人开了一下复读，这个群的发图权限就没了"，中间隔着几天没人对得上。合并语义定在
// 端口的真身里（一条 `jsonb ||`），指令层只给要改的那一项。
//
// ## 写成了才说"已经开启啦"
//
// 拆分前这两步的顺序也是先写后说，但那句回复是 fire-and-forget 的（没有 await）。这里
// await 它：出错时引擎记一条 handler_error，而不是变成一个没人接的 rejection ——
// 进程入口的 unhandledRejection 处理器是 `process.exit(1)`，一次回复失败会把持着飞书
// 长连的那个进程带走。

import {
    EqualText,
    NeedRobotMention,
    OnlyGroup,
    TextMessageLimit,
} from '@inner/shared/rules';

import type { LarkCommand, LarkCommandDeps } from '../rules/commands';

/** 文案是线上历史的一部分，逐字照搬，不要顺手改措辞。 */
const OPENED =
    '呜哇~复读功能已经开启啦！如果在群聊里看到同样的文字或表情连续出现三次的话，人家也会跟着一起复读呢！(。>︿<)_θ';
const CLOSED = '诶嘿~复读功能已经关闭啦！人家暂时就不会复读了呢 (｡•́︿•̀｡)';

function toggleRepeatCommand(open: boolean): (deps: LarkCommandDeps) => LarkCommand {
    const trigger = open ? '开启复读' : '关闭复读';

    return (deps) => (context) => ({
        rules: [EqualText(trigger), TextMessageLimit, NeedRobotMention, OnlyGroup],
        comment: trigger,
        category: 'utility',
        handler: async () => {
            await deps.store.setLarkChatPermission(context.message.chatId, {
                open_repeat_message: open,
            });
            // 拆分前这句不进话题（`replyMessage` 的 replyInThread 没传）。
            await deps.api.replyText(context.message.messageId, open ? OPENED : CLOSED, false);
        },
    });
}

export const openRepeatCommand = toggleRepeatCommand(true);
export const closeRepeatCommand = toggleRepeatCommand(false);
