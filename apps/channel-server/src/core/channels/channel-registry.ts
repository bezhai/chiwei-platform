// bot 加载链路按 bot_config.channel 分发的唯一入口。channel-server / bot 加载
// 读到一条 bot_config 记录后，按它的 channel 解析出该 channel 的三件套
// (InboundAdapter / OutboundAdapter / AddressingPolicy)。本文件 channel 无关：
// 飞书是已落地 adapter（T2），qq 是 T6 占位（必须能被识别、加载链路不挂，但
// 占位 adapter 任何方法被调用即明确抛 "not implemented"，绝不静默吞消息）。
// 凭据从 bot_config.credentials 取，由各 adapter 自己解释——本注册表不碰凭据。

import type {
    InboundAdapter,
    OutboundAdapter,
    AddressingPolicy,
    InboundMessage,
    ThreadRef,
    AddressingDecision,
} from './contracts';
import {
    LarkInboundAdapter,
    LarkOutboundAdapter,
    LarkAddressingPolicy,
} from './lark/lark-adapter';

export interface ChannelTriple {
    inbound: InboundAdapter;
    outbound: OutboundAdapter;
    addressing: AddressingPolicy;
}

// T6 之前 QQ adapter 还没实现。加载链路必须能识别 channel="qq"（不报错、能路由
// 到 qq 三件套占位），但占位的任何方法被真正调用时必须明确抛错——这正是设计
// 文档反复强调的"禁止静默丢弃"：宁可在边界炸，绝不无声吞掉一条 QQ 消息。
function notImplemented(method: string): never {
    throw new Error(`qq channel adapter not implemented yet (${method}); T6 pending`);
}

class PlaceholderInboundAdapter implements InboundAdapter {
    handleHandshake(_raw: unknown): unknown | null {
        return notImplemented('handleHandshake');
    }
    verify(_raw: unknown): boolean {
        return notImplemented('verify');
    }
    parse(_raw: unknown): InboundMessage | null {
        return notImplemented('parse');
    }
}

class PlaceholderOutboundAdapter implements OutboundAdapter {
    send(_channelChatId: string, _content: string): Promise<string> {
        return notImplemented('send');
    }
    reply(_threadRef: ThreadRef, _content: string): Promise<string> {
        return notImplemented('reply');
    }
}

class PlaceholderAddressingPolicy implements AddressingPolicy {
    decide(_msg: InboundMessage, _botIdentity: string): AddressingDecision {
        return notImplemented('decide');
    }
}

function placeholderTriple(): ChannelTriple {
    return {
        inbound: new PlaceholderInboundAdapter(),
        outbound: new PlaceholderOutboundAdapter(),
        addressing: new PlaceholderAddressingPolicy(),
    };
}

// channel -> 该 channel 三件套的工厂。新增 channel 只在这里登记一行，
// channel-server / bot 加载主流程不动（契约即此处）。
const REGISTRY: Record<string, () => ChannelTriple> = {
    lark: () => ({
        inbound: new LarkInboundAdapter(),
        outbound: new LarkOutboundAdapter(),
        addressing: new LarkAddressingPolicy(),
    }),
    // T6 之前 QQ 用占位三件套：被识别但任何方法调用即抛 not implemented。
    qq: placeholderTriple,
};

export function isKnownChannel(channel: string): boolean {
    return Object.prototype.hasOwnProperty.call(REGISTRY, channel);
}

// 按 bot_config.channel 解析三件套。未知 channel fail-closed：明确抛错而不是
// 静默返回 null —— 与项目里 paas-engine ClassifyLane fail-closed 同样的取向，
// 配错 channel 必须在加载期炸出来，不能让 bot 半死不活地起来。
export function resolveChannelTriple(channel: string): ChannelTriple {
    const factory = REGISTRY[channel];
    if (!factory) {
        throw new Error(
            `unknown channel "${channel}"; register it in channel-registry before use`,
        );
    }
    return factory();
}

// bot 加载链路用：传入已加载的 bot 列表，按每条记录的 channel 解析并装配
// 三件套，返回 bot_name -> ChannelTriple。这是 channel-registry 真正接进
// multiBotManager.initialize() 的入口——加载阶段就把 channel 解析+校验做掉，
// 未知 channel 在这里 fail-closed 抛错（与 paas-engine ClassifyLane 一致：
// 配错 channel 必须在加载期炸出来，不让 bot 半死不活地起来）。
//
// 边界（T4）：本函数只负责"加载阶段按 channel 选定+校验+装配三件套"。真正
// 用三件套处理收发消息是 T5 接线范围，不在这里。
export function resolveBotChannelTriples(
    bots: { bot_name: string; channel: string }[],
): Map<string, ChannelTriple> {
    const out = new Map<string, ChannelTriple>();
    for (const bot of bots) {
        // resolveChannelTriple 对未知 channel 抛错 —— 直接冒泡，加载失败。
        out.set(bot.bot_name, resolveChannelTriple(bot.channel));
    }
    return out;
}
