// 「复读功能」：同一句话（或同一个表情包）在群里连着出现三次，赤尾也跟着说一遍。
//
//     群消息 ──▶ 纯文字？ ──▶ 渲染成带 <at> 的一串字 ──▶ 计数 ──第 3 次──▶ 发富文本
//            └─▶ 纯表情包？ ──▶ 拿表情 key 计数 ────────第 3 次──▶ 发同一个表情包
//
// ## 谓词与拆分前逐字相同
//
//   `NeedNotRobotMention`  @ 了 bot 的消息归聊天主链路，不该被复读抢走
//   `OnlyGroup`            私聊里复读没有意义
//   会话开了 open_repeat_message（没配过一律等于关）
//
// 加上 `fallthrough: true`：复读跑完还要继续往下试后面的规则。少了它，整条规则序列会
// 在这里被截断。
//
// ## `=== 3`，不是 `>= 3`
//
// 计数只增不减（内容变了才归 1），所以 `>=` 会让第四遍、第五遍一直复读下去。严格等于
// 意味着**恰好一条流**能观察到触发点 —— 这个性质完全依赖计数器的原子性，理由和推导见
// counter.ts 的文件头。
//
// ## 计数依据是渲染之后那一串字，不是原始正文
//
// 拆分前就是这样：先 `renderLarkMentionText`，再拿结果算 md5。所以"@ 了不同的人"算不同
// 的内容（标签里的 union_id 不一样），而同一个人被 @ 时不管飞书这次给的是第几号占位符
// （`@_user_1` / `@_user_2`），算出来都一样。

import { createHash } from 'node:crypto';

import { NeedNotRobotMention, OnlyGroup } from '@inner/shared/rules';

import type { LarkCommand, LarkCommandDeps } from '../rules/commands';
import { echoPostContent, larkAtTaggedText } from './echo';

/** 连着出现几次才复读。拆分前就是 3。 */
const REPEAT_AT = 3;

/** 拆分前就是 md5 + hex。键里存的是它，所以换算法等于把切换窗口里两边的计数割裂开。 */
function contentHash(content: string): string {
    return createHash('md5').update(content).digest('hex');
}

export function repeatCommand(deps: LarkCommandDeps): LarkCommand {
    return (context) => ({
        rules: [
            NeedNotRobotMention,
            OnlyGroup,
            // 没配过 permission_config 的老群一律等于关。
            () => context.permission.open_repeat_message ?? false,
        ],
        fallthrough: true,
        comment: '复读功能',
        category: 'utility',
        handler: async (message) => {
            const chatId = context.message.chatId;

            if (message.isTextOnly()) {
                const echo = larkAtTaggedText(context.content);
                if ((await deps.repeatCounter.bump(chatId, contentHash(echo))) === REPEAT_AT) {
                    await deps.api.sendPost(chatId, await echoPostContent(deps.emoji, echo));
                }
                return;
            }

            if (message.isStickerOnly()) {
                const fileKey = message.stickerKey();
                if ((await deps.repeatCounter.bump(chatId, contentHash(fileKey))) === REPEAT_AT) {
                    await deps.api.sendSticker(chatId, fileKey);
                }
            }
            // 别的消息类型（图片、文件、视频……）两条分支都不进，**连计数都不动** ——
            // 动了的话，群里刷几张图就会把正在累积的文字计数冲掉。与拆分前一致。
        },
    });
}
