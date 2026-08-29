// 泳道交接的线协议：信封长什么样、怎么从报文里读出来、怎么变成本服务的领域对象。
//
// 交接是一次内部 HTTP —— prod 判出「这条消息属于泳道 X」之后，把信封 POST 给
// lane-inbound.ts 上的端点（投递那一侧在 lane-handoff.ts）。信封是这两侧唯一的共同
// 语言，所以「有哪些字段」「哪些必填」「怎样算不合法」只在本文件说一次，两侧都从这里
// import，谁也不再自己解析一遍。
//
// ⚠️ **跨服务镜像**：channel-server 的 infrastructure/integrations/lane-envelope.ts 上
// 有一份字段名逐字相同的定义。两个 app 是两个包，编译期对不上；两条端点各自独立演化是
// 有意的，所以这份镜像不合并，但改字段名必须两边一起改。

import { type LarkEvent } from './lark-event';

/** 交接信封。这是**跨进程的线格式**，字段名即契约。 */
export interface InboundLaneEnvelope {
    /**
     * 消息的来源渠道，恒为 `lark`。**要校验**：端点路径确实已经是渠道判别式，但一个
     * 写着 `qq` 的信封打到这里说明投递方选错了目的地，把它当 lark 事件处理下去只会在
     * 更深的地方以更难查的形状失败。channel-server 的镜像端点同样验，两侧一致。
     */
    channel: string;
    event_type: string;
    /** 全局消息 id。日志与排查用，本服务不拿它做幂等（交接不重试，见 lane-handoff.ts）。 */
    global_message_id: string;
    /** 同一条 trace 串到接收端。header 在途中可能被剥掉，所以写进信封。 */
    trace_id: string;
    /**
     * 这条消息该由哪条泳道处理。**接收端建立上下文的权威值** —— `x-ctx-lane` header 只
     * 供 sidecar 选路，落回 prod 时它与本进程泳道不等，那是正常情形。
     */
    lane: string;
    /** 投递这条消息的 bot。缺了它，下游读不到 bot 身份。 */
    bot_name: string;
    /** 飞书原始事件体，原样透传。 */
    params: unknown;
    /**
     * 「这条已经判过泳道了」，恒为 true。
     *
     * 泳道的 Service 不存在时 sidecar 把请求原样打回 prod 自己，而 prod 仍然是 prod、
     * 绑定仍然指向那条泳道 —— 没有这个标记就会被重新判定、重新投递，再被打回来，无限
     * 自投。它是那个循环的唯一阻断点，所以缺了它一律拒收（见 readEnvelope），而不是当
     * 成 false 放行。
     */
    handed_off: true;
}

/** 报文本身不成立：缺字段、类型不对、没有交接标记。重发一模一样的内容还是同样的结果。 */
export class UnprocessableEnvelope extends Error {}

/** 报文成立，但装的不是本服务认领的东西（渠道不对，或事件类型没人认领）。 */
export class UnclaimedEnvelope extends Error {}

/** 本服务在这条端点上认领的渠道。飞书的信封只可能写 lark。 */
const LARK_CHANNEL = 'lark';

// 必填字段集合与 channel-server 的镜像端点逐字对齐。少验一个字段就不再是镜像：同一份
// 报文在一侧被收下、在另一侧被拒，跨渠道排查会得到两种结论。
const REQUIRED_STRINGS = [
    'channel',
    'event_type',
    'global_message_id',
    'trace_id',
    'lane',
    'bot_name',
] as const;

/**
 * 线格式 → 信封。类型系统管不到 HTTP body，每个字段都得现验。
 *
 * 形式校验一次做完（含「已交接」标记），端点不必在解析之后再补自己的字段检查 ——
 * 补漏一处就是一条自投循环。归属（这封是不是发给本服务的）由 requireClaimed 单独答，
 * 因为它答错的后果是另一种：400 说"报文坏了"，422 说"你送错地方了"。
 */
export function readEnvelope(raw: string): InboundLaneEnvelope {
    let parsed: unknown;
    try {
        parsed = JSON.parse(raw);
    } catch {
        throw new UnprocessableEnvelope('envelope is not JSON');
    }
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
        throw new UnprocessableEnvelope('envelope is not an object');
    }
    const envelope = parsed as Record<string, unknown>;
    for (const field of REQUIRED_STRINGS) {
        if (typeof envelope[field] !== 'string' || (envelope[field] as string).length === 0) {
            throw new UnprocessableEnvelope(`envelope is missing "${field}"`);
        }
    }
    // 没有 params 的信封会一路走到解析层、解析出 null，然后被当成"处理成功"应答 ——
    // 一条静默丢失。
    if (typeof envelope.params !== 'object' || envelope.params === null) {
        throw new UnprocessableEnvelope('envelope carries no event payload');
    }
    if (envelope.handed_off !== true) {
        throw new UnprocessableEnvelope(
            'envelope is not marked as handed off; refusing it rather than risking ' +
                'an endless self-handoff',
        );
    }
    return parsed as InboundLaneEnvelope;
}

/**
 * 归属校验：这个信封是发给本服务的吗 —— 渠道要是 lark，事件类型要有人认领。
 * "投递方送错地方了，重发也还是错的"。
 *
 * **不比对泳道**。信封的 lane 与接收进程的 lane 不等是正常情形：泳道的 Service 不存在
 * 时 sidecar 把交接原样打回 prod，prod 要以信封的 lane 为上下文把它处理掉。比对泳道
 * 等于把这条退路堵死，而堵死的代价是 bot 静默变砖。
 */
export function requireClaimed(
    envelope: InboundLaneEnvelope,
    handles: (eventType: string) => boolean,
): void {
    if (envelope.channel !== LARK_CHANNEL) {
        throw new UnclaimedEnvelope(
            `this service only claims channel "${LARK_CHANNEL}", not "${envelope.channel}" ` +
                `(global_message_id=${envelope.global_message_id})`,
        );
    }
    if (!handles(envelope.event_type)) {
        throw new UnclaimedEnvelope(`no handler claims event type "${envelope.event_type}"`);
    }
}

/** 信封 → 事件。lane 与「已交接」都从信封上来，接收端不用本进程的泳道覆盖它们。 */
export function larkEventOf(envelope: InboundLaneEnvelope): LarkEvent {
    return {
        type: envelope.event_type,
        payload: envelope.params,
        botName: envelope.bot_name,
        traceId: envelope.trace_id,
        lane: envelope.lane,
        handedOff: true,
    };
}
