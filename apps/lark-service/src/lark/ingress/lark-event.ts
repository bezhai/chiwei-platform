// 三个入口汇合的地方：一条飞书事件不管从 webhook、长连还是泳道信封进来，到这里
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
    /**
     * 这条事件是另一个进程判定过泳道之后交接过来的。
     *
     * 只有泳道信封那条入口会把它置上，作用是让投影层跳过泳道判定：泳道的 Service 不
     * 存在时 sidecar 会把交接打回 prod 自己，再判一次就会算出同一条泳道、再投一次，
     * 无限自投。飞书直连的两个入口是第一次见到这条事件，不带这个标记。
     */
    handedOff?: boolean;
}

/**
 * 这条事件本身没救，再送一次也一样（载荷缺字段、内容根本不是一条消息……）。
 *
 * **没有人按类型分支**：三个入口对处理失败的处置都一样 —— 泳道交接的接收端一律
 * 应答 500，飞书那两个入口早已应答、只剩一条错误日志。所以它不改变任何处置，只是
 * 把"再送一次也没用"写在类型名上，排查时不必回去读抛它的那一行。
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
 * **报错往外抛**，不在这里吞：泳道交接的接收端靠它应答非 2xx —— 投递方不重试，2xx
 * 是"处理完了"的唯一凭据。飞书 SDK 那两个入口需要的"立刻应答、异步处理"由它们自己做
 * （见 event-sink.ts）—— 把 fire-and-forget 压进这里，交接那侧就会拿到 200 却没人处理。
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
