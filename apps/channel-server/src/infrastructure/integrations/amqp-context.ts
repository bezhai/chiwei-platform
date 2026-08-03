// 一条 AMQP 消息随身携带的跨进程上下文（lane + trace_id），消费侧的读出口径。
//
// 这一头和 rabbitmq.ts::publish 的写入是同一份约定的两侧，也和 agent-service 的
// app/runtime/propagation.py（inject_context / extract_context）是同一份跨语言约定：
// key 固定 `lane` / `trace_id`，空值写空串而不是省略 key，读回来时空串和非字符串
// 都归一成「无」。三处必须同时改，改一处就是让同一条消息在 TS worker 和 Python
// 服务里解出两个不同的 lane。
//
// 为什么单独一个模块而不是挂在 rabbitmq.ts 上：读出是不碰连接的纯函数，而
// rabbitmq.ts 是有连接状态的单例，被多个测试文件用 bun 的 mock.module 整体替换成
// 桩（mock.module 是进程级的，mock.restore() 不撤销）。纯函数挂上去，跨文件跑测试
// 时会跟着被桩掉、变成 undefined。同理 @infrastructure/lane-policy 也是因为这个
// 原因独立出去的——但那个回答的是另一个问题（本进程是不是 prod 部署），和这里
// 「这条入站消息属于哪个泳道」不是一回事。

import type { ConsumeMessage } from 'amqplib';

/**
 * header 归一：空串 / 缺失 / 非字符串（amqplib 会把 longstr 解成 Buffer）都 →
 * undefined，与 propagation.py 的 `_coerce` 同口径。lane 和 trace_id 共用这一份规则，
 * 免得两个 key 的归一慢慢漂成两套。
 */
function headerString(msg: ConsumeMessage, key: string): string | undefined {
    const raw = msg.properties.headers?.[key];
    return typeof raw === 'string' && raw ? raw : undefined;
}

/**
 * 从入站消息的 AMQP header 取 lane。空串 / 缺失 / 非字符串都 → undefined（无 lane，
 * 等价 prod）。
 *
 * 为什么只认 header：泳道队列（chat_response_{lane} / recall_{lane}）带 10s TTL +
 * DLX，下游没部泳道时消息会降级回 prod 队列由 prod worker 接手，但它**仍然属于那个
 * 泳道**——header 是唯一能穿过 TTL/DLX 降级还保持原样的载体。
 *
 * 为什么不回落 body.lane：Python 侧把「header 明确写空」和「header 压根没写」都归一
 * 成 None，消费侧无从区分；回落 body 会把上游已判定为 prod 的消息错误复活成泳道消息。
 *
 * 为什么不回落 env LANE：prod worker 收到的很可能正是降级回来的泳道消息，env 兜底
 * 会把它错判成 prod，恰好毁掉这个机制要修的能力。
 *
 * 部署窗口内的在途旧消息没有 header，按无 lane 处理、当 prod 走——已接受的降级。
 */
export function laneFromMessage(msg: ConsumeMessage): string | undefined {
    return headerString(msg, 'lane');
}

/**
 * 从入站消息的 AMQP header 取 trace_id，归一规则同 lane。
 *
 * 消费侧据此把整条处理跑在同一条 trace 下：worker 自己再 publish 时，publish 会从
 * AsyncLocalStorage 取 trace_id 写回 header（见 rabbitmq.ts::publish）。不恢复就是
 * 每跳一次换一条 trace，重投 / 降级这些最需要追的路径反而追不了。
 *
 * 取不到时返回 undefined 而不是空串：调用方拿它去 createContext，undefined 会生成
 * 一个新 traceId，本次处理内部至少是自洽的一条链；空串会让下游的 trace 字段全空。
 */
export function traceIdFromMessage(msg: ConsumeMessage): string | undefined {
    return headerString(msg, 'trace_id');
}
