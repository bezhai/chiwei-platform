// 三个入口汇合的地方：一条飞书事件不管从 webhook、长连还是泳道队列进来，到这里
// 都长成同一个样子，然后按类型交给认领它的人。
//
// 这里**没有注册表**。拆分前这一步是一个进程级的可变 Map，由装饰器在 import 期
// 往里塞函数：谁注册了什么只有运行时才知道，类型是 `(params: any) => Promise<void>`，
// 测试之间还要互相 clear。现在处理表是**装配时传进来的一个普通对象** —— 编译期
// 就能看见谁认领了哪些事件，测试各自带自己的表，进程里没有共享状态。

import { context } from '@inner/shared/middleware';

/** 一条待处理的飞书事件。三个入口各自负责把自己那套信封拆成这个形状。 */
export interface LarkEvent {
    /** 飞书的 event_type，如 `im.message.receive_v1`。 */
    type: string;
    /** 飞书原始事件体。由认领它的 handler 自己解释。 */
    payload: unknown;
    /** 处理这条事件的 bot。 */
    botName: string;
    traceId?: string;
    /** 这条事件该在哪条泳道处理。prod 不填。 */
    lane?: string;
}

/**
 * 这条事件本身没救，重试多少次都一样（载荷缺字段、内容根本不是一条消息……）。
 *
 * 存在的理由是**队列那条路要区分"这次不行"和"永远不行"**：前者退回去重投，后者
 * 重投就是让它在队头堵死整条泳道。抛这个类型等于对调用方说"别重试了"。
 * 普通 Error 一律按"这次不行"处理 —— 默认重试比默认丢弃安全。
 */
export class UnprocessableLarkEvent extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'UnprocessableLarkEvent';
    }
}

export type LarkEventHandler = (event: LarkEvent) => Promise<void>;

/** 事件类型 → 认领它的人。装配时一次性建好，之后只读。 */
export type LarkEventHandlers = Readonly<Record<string, LarkEventHandler | undefined>>;

/**
 * 在请求上下文里把事件交给它的 handler。
 *
 * **报错往外抛**，不在这里吞：泳道消费者要靠它决定 ack 还是重投。飞书 SDK 那两个
 * 入口需要的"立刻应答、异步处理"由它们自己做（见 event-sink.ts）—— 把 fire-and-forget
 * 压进这里，泳道那条路就再也没法知道处理失败了。
 */
export async function deliverLarkEvent(
    event: LarkEvent,
    handlers: LarkEventHandlers,
): Promise<void> {
    const handler = handlers[event.type];
    if (!handler) {
        // 飞书会推很多我们没订阅的类型，不是错误；但要能看见，否则"某个事件从来
        // 没被处理过"永远查不出来。
        console.warn(`[lark-ingress] nobody handles event type ${event.type}`);
        return;
    }

    // bot 身份、泳道、trace 传不进 handler 的参数（handler 只吃事件本身），下游
    // 的 bot 目录 / 规则 / 发 MQ 都从上下文读。
    const ctx = context.createContext(event.traceId, {
        botName: event.botName,
        lane: event.lane,
    });
    await context.run(ctx, () => handler(event));
}
