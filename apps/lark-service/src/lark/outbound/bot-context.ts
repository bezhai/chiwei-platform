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
// ## trace 由调用方决定接不接，两条出站链的答案不同
//
// 发消息那条链**不传** traceId，于是每段回复各自铸一条新 trace。这是照搬拆分前
// chat-response-worker 的行为，不是决定 —— 拆分前它调的是
// `createContext(botName, undefined, lane)`，traceId 那一格传的就是 undefined。恢复它
// 是纯改进（入站和出站能串成一条链），但那是另一个议题：这一刀的验收口径是"行为与
// 拆分前一致"，顺手改会让"行为变了没有"这个判断多一个变量。
//
// 撤回那条链**要传**，同样是照搬：拆分前 recall-worker 从 AMQP header 恢复 trace_id
// 并把整条处理跑在里面，因为它自己还会往外发一条延时重投，而 publish 的 trace_id 取自
// AsyncLocalStorage —— 不接上就是每次重试换一条 trace。

import { context } from '@inner/shared/middleware';

import type { LarkSpeakAs } from './deliver';

export const larkSpeakAs: LarkSpeakAs = (who, say) =>
    context.run(
        context.createContext(who.traceId, { botName: who.botName, lane: who.lane }),
        say,
    );
