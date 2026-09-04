// 别人在飞书撤回一条消息之后，本服务要做的唯一一件事：把公共层那一行标上撤回时刻。
//
// 不做这件事的后果不是"少了一条记录"—— 赤尾读会话、算未读、找东西读都从公共层取，
// 那一行原样留着，她就照样看得到一条对面已经撤掉的消息，还可能接一句。她看到的会话
// 和主人看到的会话不是同一个。
//
// ## 这条链上没有任何重投，所以每一种失败都必须自己走到终态
//
// 飞书要求立刻应答，处理跑在没人跟踪的 Promise 里（见 ingress/event-sink.ts），进程
// 收到停止信号就直接退出。于是：
//
//   - **不做进程内休眠重试。** 睡在那儿的重试既不会完成、也不会留下"放弃了"的记录，
//     那比不重试更糟 —— 它看起来像有保障。
//   - **不往外抛。** 应答早发出去了，抛出去只会变成入口那层一条泛泛的错误日志，反而
//     把"是定位不到还是库炸了"这个区分丢掉。
//
// **接受的代价**：撤回事件和原消息的投影真撞上并发窗口时，这条消息会永久保持可见 ——
// 她会看到一条别人已经撤掉的消息，而且没有任何东西会再来补一次。窗口极小（人点撤回是
// 秒级，投影是毫秒级），日志里查得到。要真正堵上得让对账去扫历史，那是另一件事。
// 库报错落在同一个形状上：那一行也就永久停在"没撤回"。
//
// ## 拿不到"是谁撤的"
//
// 报文里只有一个角色枚举（消息本人 / 群主 / 群管理员 / 企业管理员），没有撤回者的身份
// （见 message/wire.ts 的 LarkRecallEvent）。所以库里记的是"这条被撤了"，不是"谁撤的"。
//
// ## 落库的撤回时刻是收到事件那一刻，不是报文给的 recall_time
//
// **这是一个已知的、故意的保守选择**，登记在此：等拿到实证样本、确认了单位，可以改回
// 用飞书给的值。三条理由：
//
//   - **单位拿不准。** 飞书文档的示例值是 13 位（毫秒形态），而这里原先按秒解析，
//     **仓里没有任何实证样本**能裁决到底是哪一种。
//   - **按错一边的后果不对称。** 按秒解析一个 13 位值，得到的是几万年后的时刻；它会被
//     "撤回不可能发生在我们听说它之后"这条因果检查退回 receivedAt，于是看起来"没事"——
//     实际是把飞书给的时刻整个丢掉，还被首写保留永久固定，而且没有任何人会察觉。
//   - **用收到时刻是可证明正确的。** 它就是本进程听说这件事的那一刻，跟真实撤回时刻只
//     差一次网络推送。而这一列的用途是"这条还在不在会话里"，时刻本身只是记录，差几百
//     毫秒不影响任何判断。
//
// **但飞书给的原始值不能丢**：recall_time 原样（不做任何解析）记进成功那条日志。prod
// smoke 时就能从日志里拿到第一个实证样本，确认单位之后再决定要不要改用它。
//
// `receivedAt` 由入口在**应答之前**取好（见 ingress/lark-event.ts 的 receivedAt）。在这里
// 现取的话，落下去的就是"我们什么时候腾出手处理它"，而不是"我们什么时候听说这件事"。

import type { LarkRecallEvent } from './message/wire';
import type { LarkTables } from './projection/tables';

export interface LarkRecallDeps {
    /**
     * 两条语句：按飞书消息标识查映射、把公共层那一行标成撤回。
     *
     * 飞书的消息标识不在 common_message 上，在 lark_message 这张映射表上，所以定位
     * 必须先过一次 larkMessage —— 撤回事件里没有任何公共层的 id。
     */
    store: Pick<LarkTables, 'larkMessage' | 'markCommonMessageRecalled'>;
}

export async function receiveLarkRecall(
    deps: LarkRecallDeps,
    recall: LarkRecallEvent,
    receivedAt: Date,
): Promise<void> {
    const omId = recall.message_id;
    if (!omId) {
        // 没有消息标识就无从定位，重发一模一样的报文也一样。日志里带上会话标识和角色
        // ——那是这份报文里仅剩的两条能用来找现场的线索。
        console.warn(
            '[lark-recall] recall event carries no message id, nothing to mark: ' +
                `chat_id=${recall.chat_id ?? '-'} recall_type=${recall.recall_type ?? '-'}`,
        );
        return;
    }

    // 不解析 recall_time —— 理由见文件头。落的是我们听说这件事的那一刻。
    const recalledAt = receivedAt;

    try {
        const mapping = await deps.store.larkMessage(omId);
        if (!mapping) {
            // 撤回事件与原消息的投影并发（重试能救），或者那条消息本来就没落库、投影
            // 当时失败了（重试无用）—— 这两种在这里分不开，所以一次尝试就放弃。
            console.warn(
                '[lark-recall] no common message for a recalled lark message; giving up ' +
                    `after one attempt: om_id=${omId} chat_id=${recall.chat_id ?? '-'} ` +
                    `recall_type=${recall.recall_type ?? '-'} ` +
                    `recalled_at=${recalledAt.toISOString()}`,
            );
            return;
        }

        const written = await deps.store.markCommonMessageRecalled(
            mapping.common_message_id,
            recalledAt,
        );
        if (!written) {
            console.info(
                `[lark-recall] ${mapping.common_message_id} (${omId}) already carries a ` +
                    'recall time; keeping the first one',
            );
            return;
        }
        // recall_time 原样带出来：落库用的不是它，这条日志就是它唯一的去处，也是
        // prod 上判定它单位的第一个实证样本来源。不解析、不校验、垃圾值照记。
        console.info(
            `[lark-recall] marked ${mapping.common_message_id} (${omId}) recalled at ` +
                `${recalledAt.toISOString()}; lark said recall_time=${recall.recall_time ?? '-'}`,
        );
    } catch (error) {
        // 跟「定位不到」分开记：那是一个结论（这条消息不在公共层），这里是这一跳没跑成
        // （库连不上、死锁……）。混成一条日志的话，排查时分不出该去查投影还是查数据库。
        console.error(
            `[lark-recall] database error while marking om_id=${omId} recalled; ` +
                'it stays visible to her:',
            error,
        );
    }
}
