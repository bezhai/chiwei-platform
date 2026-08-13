// channel-server 的出站消费拥有哪些 channel。
//
// 已知 channel 是一份显式清单，不动态发现：动态发现意味着队列可能压根没被声明，
// 而 topic exchange 上没有绑定的 routing key 是静默丢消息。Python 侧
// （app/infra/rabbitmq.py::KNOWN_CHANNELS）维护自己那一份，跨语言没法共享 ——
// 加新渠道时两边都要改。
//
// 收窄走 Dynamic Config 而不是 env：Release env 会被 deploy 的 POST 清空，长期开关
// 放那里会在某次部署之后悄悄失效，而这个开关失效的表现是「channel-server 又开始抢
// lark-service 的消息」——RabbitMQ 轮询把流量随机劈成两半，不报错、不留痕。

import { DynamicConfig } from '@inner/shared';

/**
 * cutover 窗口里 channel-server 出站消费拥有的渠道。
 *
 * lark 还在这里，是因为清理必须在切换稳定之后：代码删了就只能回滚镜像版本，
 * 而那会连带回滚 common 层的其他改动。Task F 关闭窗口时把 lark 从这里删掉 ——
 * 那一刻起，下面那条「首次读不到就拥有全部」的回落本身也不再危险。
 */
export const CHANNEL_SERVER_CHANNELS = ['lark', 'qq'] as const;

export const OUTBOUND_CHANNELS_KEY = 'channel_server_outbound_channels';

/**
 * 解析配置值，**不做兜底**。
 *
 * 返回 null = 「这份配置没给出有效指令」（没配 / 空 / 全填错）。兜底成什么取决于
 * 之前有没有读到过有效值，那是时序问题，归 ChannelOwnership 管。
 */
export function parseOwnedChannels(raw: string): string[] | null {
    const known = new Set<string>(CHANNEL_SERVER_CHANNELS);
    const seen = new Set<string>();
    const owned: string[] = [];
    const unknown: string[] = [];

    for (const piece of raw.split(',')) {
        const name = piece.trim().toLowerCase();
        if (!name) continue;
        if (!known.has(name)) {
            unknown.push(name);
            continue;
        }
        if (seen.has(name)) continue;
        seen.add(name);
        owned.push(name);
    }

    if (unknown.length > 0) {
        console.warn(
            `[OutboundChannels] ${OUTBOUND_CHANNELS_KEY} names unknown channels ` +
                `[${unknown.join(', ')}]; known are [${CHANNEL_SERVER_CHANNELS.join(', ')}]`,
        );
    }
    return owned.length > 0 ? owned : null;
}

/**
 * 「我拥有哪些 channel」的权威解析，带 last-known-good。
 *
 * 「读不到就拥有全部」只在**移交之前**成立。lark 交给 lark-service 之后，一次
 * Dynamic Config 瞬断就会让这个 worker 重新订阅 lark 的出站队列——两个消费者守着
 * 同一条队列，RabbitMQ 轮询把流量随机劈成两半，不报错、不留痕。所以时序上分两种：
 *
 * - **从没成功读到过**：没有可依据的结论，保持现状行为（拥有全部）。首次部署时
 *   配置还没建、或者操作者压根没打算收窄，回落到空集等于 worker 变砖。
 * - **成功读到过之后再读不到**：记得上次的结论，绝不自己变宽。变宽是危险方向，
 *   必须由一次显式且有效的配置来驱动 —— 把 channel 加回来照常有效（回滚路径）。
 *
 * DynamicConfig.get 把 fetch 失败吞成默认值（dynamic-config/index.ts::fetchSnapshot
 * catch 之后 return {}），所以「读失败」和「没配这个 key」在这层是同一个信号：空串。
 * read() 自己抛异常也按同一条路处理。三者的共同点是「这次没拿到有效指令」，而对
 * 「不许自己变宽」这条不变量来说，它们的处置完全一样。
 *
 * 残留风险，写在这里免得下次有人以为它被解决了：last-known-good 只活在进程内存里。
 * 进程重启 + 恰好那一刻 Dynamic Config 读不到，就会退回 bootstrap 回落（拥有全部），
 * 直到下一次 reconcile 读到配置为止。真正关掉这个窗口的是 Task F —— lark 从
 * CHANNEL_SERVER_CHANNELS 里删掉之后，bootstrap 回落再宽也宽不到 lark 上。
 */
export class ChannelOwnership {
    private lastGood: string[] | null = null;

    constructor(private readonly read: () => Promise<string>) {}

    async resolve(): Promise<string[]> {
        let parsed: string[] | null = null;
        let failure: string | null = null;
        try {
            parsed = parseOwnedChannels(await this.read());
        } catch (err) {
            failure = (err as Error).message;
        }

        if (parsed !== null) {
            this.lastGood = parsed;
            return parsed;
        }

        const why = failure ?? `${OUTBOUND_CHANNELS_KEY} is unset or names no known channel`;
        if (this.lastGood !== null) {
            // 稳定 event 名，make logs KEYWORD=outbound_channels_config_unavailable 可捞。
            console.error(
                `[OutboundChannels] outbound_channels_config_unavailable: ${why}; ` +
                    `keeping last known good [${this.lastGood.join(', ')}] — widening back ` +
                    `would re-subscribe queues that were already handed off`,
            );
            return [...this.lastGood];
        }

        console.warn(
            `[OutboundChannels] outbound_channels_bootstrap_default: ${why}; ` +
                `never read a valid value, owning all of ` +
                `[${CHANNEL_SERVER_CHANNELS.join(', ')}]`,
        );
        return [...CHANNEL_SERVER_CHANNELS];
    }
}

const dynamicConfig = new DynamicConfig();

// 进程级单例：last-known-good 要跨 reconcile 存活，每次新建一个就等于没有记忆。
const ownership = new ChannelOwnership(() => dynamicConfig.get(OUTBOUND_CHANNELS_KEY, ''));

export function loadOwnedChannels(): Promise<string[]> {
    return ownership.resolve();
}
