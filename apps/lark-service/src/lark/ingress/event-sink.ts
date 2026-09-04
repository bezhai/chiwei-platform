// 飞书 SDK 两个入口（HTTP webhook 与长连）共用的回调形状。
//
// 一条硬约束定死了这里的写法：**飞书要求立刻应答**，慢一点就当我们没收到、重推。
// 所以回调是同步返回 `{}`、处理异步跑。代价要说清楚：应答一发出去，这条消息在
// 飞书那边就算送达了 —— 我们这侧后面处理失败，平台不会再推一次。所以入口的连续
// 性是切换时的硬要求，不能出现"两边都不接"的窗口。
//
// 审计（原始报文落库）是旁路：先记、失败只打日志。审计库挂了不该让消息处理不了。

import type { LarkEvent } from './lark-event';

/** SDK 要求回调同步返回一个对象，内容不重要，空对象即是 ack。 */
type LarkAck = Record<string, never>;

export interface LarkEventSinkPorts {
    /** 这个入口是替哪个 bot 接的。 */
    botName: string;
    /** 原始报文落库，只为可追溯。 */
    record: (payload: unknown) => Promise<void>;
    deliver: (event: LarkEvent) => Promise<void>;
}

export interface LarkEventSink {
    onEvent(payload: unknown): LarkAck;
    onCardAction(payload: unknown): LarkAck;
}

export function createLarkEventSink(ports: LarkEventSinkPorts): LarkEventSink {
    const accept = (type: string, payload: unknown): LarkAck => {
        // 应答之前就把时刻取好。处理跑在下面那个没人跟踪的 Promise 里，等它轮到自己
        // 再取，拿到的是"我们什么时候腾出手"，不是"飞书什么时候把这件事送过来"——
        // 撤回事件的撤回时刻在报文缺失时正是拿它兜底（见 lark/recall-message.ts）。
        const receivedAt = new Date();
        ports.record(payload).catch((error) => {
            console.error('[lark-ingress] failed to record the raw event:', error);
        });
        ports.deliver({ type, payload, botName: ports.botName, receivedAt }).catch((error) => {
            // 应答早就发出去了，这里再抛只会变成 unhandled rejection 打死进程。
            // 能做的只有留下一条查得到的日志。
            console.error(
                `[lark-ingress] ${type} for ${ports.botName} failed after ack:`,
                error,
            );
        });
        return {};
    };

    return {
        onEvent: (payload) =>
            accept((payload as { event_type?: string })?.event_type || 'unknown', payload),
        // 卡片回调走另一条 SDK 回调，报文里没有 event_type，类型由入口本身决定。
        onCardAction: (payload) => accept('card.action.trigger', payload),
    };
}
