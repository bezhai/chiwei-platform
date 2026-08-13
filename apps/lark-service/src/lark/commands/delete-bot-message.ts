// 「撤回」：用户回复赤尾说过的某一句、@ 她说「撤回」，她把**那一句**撤掉。
//
//     @bot 撤回（回复着 om_parent）
//        └──▶ 有没有被回复的那条 ──▶ 查它 ──▶ 是我发的吗 ──▶ 撤
//                     └── 任何一步不成立 ──▶ 「撤回失败: <原因>」
//
// ## 这不是安全审计那条撤回
//
// 出站那条（../outbound/recall.ts）是 agent-service 判定内容违规之后自动撤，走 MQ、
// 按 session 批量撤、要写 safety 终态。这一条是用户主动敲的一条指令，只撤一条、不碰
// 台账。两者除了"最后都调 recall"之外没有任何共同点，别把它们接到一条链上。
//
// ## 归属校验比的是 app_id
//
// 飞书对 **bot 自己发的**消息返回的 `sender.id` 是 app_id（不是 union_id），所以判据
// 就是它跟本次事件的 app_id 相等。这里刻意用逐消息的 `context.appId` 而不是任何进程
// 常量：同一个进程同时跑着好几个飞书应用，用错来源就能撤同群别家 bot 的消息。
//
// ## 谓词与拆分前逐字相同，包括**没有 category**
//
//   `EqualText('撤回')`  整句相等
//   `TextMessageLimit`   只认纯文本
//   `NeedRobotMention`   群里必须 @ 到我
//
// 上游十条指令里只有这一条没声明 `category`，于是它对人设 bot 也生效（引擎只在规则
// 声明了 category 时才按 botRole 过滤）。顺手补一个 `category: 'utility'` 会让赤尾从此
// 撤不了自己的消息，而现象只是"敲了撤回没反应"。
//
// ## 全部失败都收敛成一句话，绝不外溢
//
// 与拆分前一致：整段包在 try 里，任何一步失败都翻成「撤回失败: <原因>」回给用户，
// handler 本身永远正常返回。那一句**进话题**（inThread=true），也照搬。

import { EqualText, NeedRobotMention, TextMessageLimit } from '@inner/shared/rules';

import type { LarkCommandContext } from '../rules/command-context';
import type { LarkCommand, LarkCommandDeps } from '../rules/commands';

/** 三种拒绝的说法。逐字照搬，它们会原样出现在「撤回失败: 」后面。 */
const NO_PARENT = '没有父消息，无法撤回';
const PARENT_GONE = '父消息为空，无法撤回';
const NOT_MINE = '只能撤回机器人自己发送的消息';

export function deleteBotMessageCommand(deps: LarkCommandDeps): LarkCommand {
    return (context) => ({
        rules: [EqualText('撤回'), TextMessageLimit, NeedRobotMention],
        comment: '撤回消息',
        handler: async () => {
            try {
                await recallTheRepliedMessage(deps, context);
            } catch (error) {
                await apologise(deps, context, error);
            }
        },
    });
}

async function recallTheRepliedMessage(
    deps: LarkCommandDeps,
    context: LarkCommandContext,
): Promise<void> {
    const parentId = context.message.parentId;
    if (!parentId) throw new Error(NO_PARENT);

    // 端口对查询只认两种答案：查不到返回 null、出错抛。上游那三条判断（items 不在 /
    // 空数组 / 第一项是空）在端口那一层已经归一成 null。
    const parent = await deps.api.getMessage(parentId);
    if (!parent) throw new Error(PARENT_GONE);

    if (parent.senderId !== context.appId) throw new Error(NOT_MINE);

    await deps.api.recall(parentId);
}

/**
 * 把失败翻成一句人话。**自己再失败也不外溢**：用户那边已经无从补救，往上抛只会让
 * 引擎多记一条 handler_error。与 ../photo/send-photo.ts 的 apologise 同一个处理。
 */
async function apologise(
    deps: LarkCommandDeps,
    context: LarkCommandContext,
    error: unknown,
): Promise<void> {
    const why = error instanceof Error ? error.message : '未知错误';
    console.error('[lark-recall-command] 撤回 failed:', error);
    try {
        await deps.api.replyText(context.message.messageId, `撤回失败: ${why}`, true);
    } catch (replyError) {
        console.error(
            '[lark-recall-command] could not even tell the user 撤回 failed:',
            replyError,
        );
    }
}
