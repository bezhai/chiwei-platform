// LarkSpeakAs 的真身：把"这个 bot 正在说话"变成一段真正的作用域。
//
// 出站是并发消费 —— 同一个进程里几条消息交错跑。用一个进程级的"当前 bot"字段，
// 两条消息会互相改掉对方的值，结果是从错的人设发出去，而且发出去之后从文本上完全
// 看不出错。AsyncLocalStorage 让每条消息各有一份。
//
// 单独一个文件而不是写在装配根里：这一跳是**契约**（飞书客户端池读的就是这里写进去
// 的 botName，见 sdk-lark-api.ts），接错了整条出站都会从同一个 bot 发出去。契约要有
// 测试指着。
//
// ## trace 不从消息 header 恢复
//
// AMQP header 里有 trace_id（agent-service 写的），这里**没有**读它，每段回复各自
// 铸一条新 trace。这是照搬拆分前 chat-response-worker 的行为，不是决定 ——
// 拆分前它调的是 `createContext(botName, undefined, lane)`，traceId 那一格传的就是
// undefined。恢复它是纯改进（入站和出站能串成一条链），但那是另一个议题：这一刀的
// 验收口径是"行为与拆分前一致"，顺手改会让"行为变了没有"这个判断多一个变量。

import { context } from '@inner/shared/middleware';

import type { LarkSpeakAs } from './deliver';

export const larkSpeakAs: LarkSpeakAs = (who, say) =>
    context.run(
        context.createContext(undefined, { botName: who.botName, lane: who.lane }),
        say,
    );
