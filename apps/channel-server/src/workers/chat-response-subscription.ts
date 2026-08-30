// chat-response-worker 的出站订阅面：本服务出站只有 QQ，就订 chat_response_qq 这一条。
//
// 出站队列按 channel 分区，因为 owner 按 channel 拆成了两个服务：飞书归 lark-service，
// QQ 归本服务。共用一条队列意味着 RabbitMQ 轮询把回复随机劈成两半，两个服务各发一半
// —— 不报错、不留痕。撤回同理归 lark-service 自己消费，本服务不起 recall 消费者。
//
// 订阅是无条件的：没有开关、没有运行期收窄、没有 drain 屏障。那套是飞书从本服务拆到
// lark-service 的切流期脚手架 —— 两个服务同时订着 chat_response，靠 Dynamic Config
// 决定谁消费哪些渠道，再用 drain 屏障做无双发的移交。移交完成之后本服务只剩一个渠道，
// 所有分支输出相同，机制随开关一起删了。下次再拆渠道时按那时的实际形态重新设计。

import {
    CHAT_RESPONSE,
    channelRoute,
    laneQueue,
    type MessageHandler,
    type Route,
} from '@inner/shared/mq';

/** 本服务出站消费的渠道。飞书移交给 lark-service 之后只剩它。 */
export const CHANNEL_SERVER_OUTBOUND_CHANNEL = 'qq';

/** 订阅要用到的 MQ 表面，就这两件事。 */
export interface ChatResponseSubscriptionPort {
    /** 声明队列与绑定。订一条没声明的队列等于守着空气。泳道后缀由端口内部按 LANE 加。 */
    declareRoute(route: Route): Promise<void>;
    consume(queue: string, handler: MessageHandler): Promise<void>;
}

export interface ChatResponseSubscriptionOptions {
    port: ChatResponseSubscriptionPort;
    /**
     * 本进程所在的泳道。**必须与 declareRoute 用的是同一个来源**（生产上两边都读
     * env 的 LANE）：声明的是 A、订阅的是 B 的话，两步都"成功"，就是一条消息都收不到。
     */
    lane?: string;
    /**
     * 造一个 handler，它只处理 accepts 放行的 channel。队列绑定和 payload 打架时
     * 以队列为准 —— 生产者分流错了要立刻暴露，而不是被顺手处理掉。
     */
    handlerFor: (accepts: (channel: string) => boolean) => MessageHandler;
}

/** 订上本服务那条出站队列，返回队列名（进程入口打日志用）。 */
export async function subscribeChatResponse(
    options: ChatResponseSubscriptionOptions,
): Promise<string> {
    const route = channelRoute(CHAT_RESPONSE, CHANNEL_SERVER_OUTBOUND_CHANNEL);
    const queue = laneQueue(route.queue, options.lane);

    await options.port.declareRoute(route);
    await options.port.consume(
        queue,
        options.handlerFor((channel) => channel === CHANNEL_SERVER_OUTBOUND_CHANNEL),
    );

    return queue;
}
