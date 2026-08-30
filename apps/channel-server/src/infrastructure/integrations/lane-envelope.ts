// 泳道交接的线协议：信封长什么样、打到哪条路径上。
//
// 交接是一次内部 HTTP —— prod 判出「这条消息属于泳道 X」之后，带 `x-ctx-lane: X` 把
// 信封 POST 给目标泳道，由 lane-sidecar 解析目的地（投递侧在 lane-handoff.ts，接收侧
// 在各渠道的 lane-inbound.ts）。信封和路径是这两侧唯一的共同语言，所以只在本文件定义
// 一次，两侧都从这里 import。
//
// ⚠️ **跨服务镜像**：lark-service 的 lark/ingress/lane-envelope.ts 上有一份字段名逐字
// 相同的定义。两个 app 是两个包，编译期对不上；两条端点各自独立演化是有意的，所以这份
// 镜像不合并，但改字段名必须两边一起改。

/** 交接信封。这是**跨进程的线格式**，字段名即契约。 */
export interface InboundLaneEnvelope {
    /**
     * 消息的来源渠道。接收端据此判断「这条信封是不是发给我的」——
     * /api/internal/qq/lane-inbound 只认 qq（见 plugins/qq/lane-inbound.ts）。
     */
    channel: string;
    event_type: string;
    /** 全局消息 id。日志与排查用，交接不重试，所以不拿它做幂等。 */
    global_message_id: string;
    /** 同一条 trace 串到接收端。header 在途中可能被剥掉，所以写进信封。 */
    trace_id: string;
    /**
     * 这条消息该由哪条泳道处理。**接收端建立 context 的权威值** —— `x-ctx-lane` header
     * 只供 sidecar 选路，落回 prod 时它与本进程泳道不等，那是正常情形。
     */
    lane: string;
    /**
     * 投递这条消息的 bot 名。接收端据此注入 context.botName，否则入站后半段
     * （context.getBotName()）拿不到 bot 身份。
     */
    bot_name: string;
    /**
     * 「这条已经判过泳道了」，恒为 true。
     *
     * 接收端见到它就不再做泳道判定 —— sidecar 在目标泳道的 Service 不存在时会把请求
     * 原样打回 prod 自己，再判一次泳道就会再交接一次，无限自投。这是那个循环的唯一
     * 阻断点，所以它是线协议的一部分，缺了它一律拒收。
     */
    handed_off: true;
    /** 原始平台事件 params，透传给目标泳道的同一套入站处理。平台无关层不解释它。 */
    params: unknown;
}

/**
 * 信封的 HTTP 接收端路径。一个渠道一条，因为一条端点只认一份契约：
 * `/api/internal/{channel}/inbound` 收的是该渠道的原始入站消息，
 * `/api/internal/{channel}/lane-inbound` 收的是已经判过泳道的信封。
 *
 * ⚠️ **跨服务契约**：投递方（handOffToLane）和接收方（各渠道 runtime 注册路由）必须
 * 拼出逐字相同的路径。改名只改一边的症状是投递方拿 404，而 404 算投递失败 —— 那条
 * 消息就此没人处理。所以只在这里定义一次。
 */
export function laneInboundPath(channel: string): string {
    return `/api/internal/${channel}/lane-inbound`;
}
