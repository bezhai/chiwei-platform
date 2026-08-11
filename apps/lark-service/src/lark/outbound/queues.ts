// lark-outbound 订的两条队列，以及各自的消息交给谁处理。
//
// 单独成文件而不是写在 outbound.ts 的 main() 里，是为了让「订哪几条」可以被断言：
// 装配藏在进程入口里的时候，把撤回那条 binding 删掉不会让任何测试变红，而它的表现
// 是"赤尾说错话之后没人来撤" —— 队列在涨、进程健康、日志干净。
//
// 这里只做装配：哪条队列归谁在各自的 *-queue.ts，什么时候订、什么时候交还在
// subscription.ts，真正把话送出去 / 把消息撤掉在 deliver.ts / recall.ts。

import {
    larkRecallBinding,
    type LarkRecallChannel,
    type LarkRecallConsumerDeps,
} from './recall-queue';
import {
    larkChatResponseBinding,
    type LarkResponseChannel,
    type LarkResponseConsumerDeps,
} from './response-queue';
import type { OutboundQueueBinding } from './subscription';

export interface LarkOutboundQueueDeps {
    /** 两条队列共用的 MQ 表面：ACK 这两件事，外加撤回的延时重投。 */
    amqp: LarkResponseChannel & LarkRecallChannel;
    /** 把这一段送到飞书。见 deliver.ts。 */
    deliver: LarkResponseConsumerDeps['deliver'];
    /** 撤掉这一条。见 recall.ts。 */
    recall: LarkRecallConsumerDeps['recall'];
    observeQueueDelay: LarkResponseConsumerDeps['observeQueueDelay'];
}

/**
 * 出站的全部队列。**回复和撤回同属一件事**：流量差着几个数量级、没有分开扩缩容的
 * 理由，所以共用一个进程、一个客户端池、一把消费开关。
 */
export function larkOutboundQueues(deps: LarkOutboundQueueDeps): OutboundQueueBinding[] {
    return [
        larkChatResponseBinding({
            amqp: deps.amqp,
            deliver: deps.deliver,
            observeQueueDelay: deps.observeQueueDelay,
        }),
        larkRecallBinding({ amqp: deps.amqp, recall: deps.recall }),
    ];
}
