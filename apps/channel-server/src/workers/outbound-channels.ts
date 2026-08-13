// channel-server 的出站消费拥有哪些 channel。
//
// 已知 channel 是一份显式清单，不动态发现：动态发现意味着队列可能压根没被声明，
// 而 topic exchange 上没有绑定的 routing key 是静默丢消息。Python 侧
// （app/infra/rabbitmq.py::KNOWN_CHANNELS）维护自己那一份，跨语言没法共享 ——
// 加新渠道时两边都要改。
//
// 收窄走 Dynamic Config 而不是 env：Release env 会被 deploy 的 POST 清空，长期开关
// 放那里会在某次部署之后悄悄失效，而这个开关失效的表现是「两个服务同时守着同一条
// 出站队列」——RabbitMQ 轮询把流量随机劈成两半，不报错、不留痕。

import { DynamicConfig } from '@inner/shared';

/**
 * channel-server 出站消费能拥有的渠道。
 *
 * 飞书移交给 lark-service 之后只剩 QQ —— 本服务连飞书的出站能力都没有了（插件、
 * 渲染、映射表全在 lark-service），所以这份清单不只是"不该收"，是"收了也发不出去"。
 *
 * 只剩一个渠道意味着下面那套收窄逻辑当前只有一种可能的输出。留着它是因为它是
 * OutboundSubscriptions 的运行期输入：下一次把某个渠道拆出去时，移交仍然要能在不
 * 重启进程的前提下做（先摘认领、再 drain 屏障）。
 */
export const CHANNEL_SERVER_CHANNELS = ['qq'] as const;

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
 * 「读不到就拥有全部」的兜底目标是 CHANNEL_SERVER_CHANNELS，也就是**本进程有能力
 * 消费的全部**。飞书从这份清单里删掉之后，兜底再宽也宽不到别的服务的队列上 ——
 * 它不再是"最危险的一次回落"，而是"没拿到指令就守住自己的全部"。时序上分两种：
 *
 * - **从没成功读到过**：没有可依据的结论，拥有全部。首次部署时配置还没建、或者
 *   操作者压根没打算收窄，回落到空集等于 worker 变砖。
 * - **成功读到过之后再读不到**：记得上次的结论，绝不自己变宽。移交进行中时，一次
 *   Dynamic Config 瞬断不该让这个 worker 把已经交出去的队列重新订回来 —— 那是两个
 *   消费者守着同一条队列，RabbitMQ 轮询把流量随机劈成两半，不报错、不留痕。
 *
 * DynamicConfig.get 把 fetch 失败吞成默认值（dynamic-config/index.ts::fetchSnapshot
 * catch 之后 return {}），所以「读失败」和「没配这个 key」在这层是同一个信号：空串。
 * read() 自己抛异常也按同一条路处理。三者的共同点是「这次没拿到有效指令」，而对
 * 「不许自己变宽」这条不变量来说，它们的处置完全一样。
 *
 * 残留风险，写在这里免得下次有人以为它被解决了：last-known-good 只活在进程内存里。
 * 进程重启 + 恰好那一刻 Dynamic Config 读不到，就会退回 bootstrap 回落（拥有全部），
 * 直到下一次 reconcile 读到配置为止。清单里只剩一个渠道的时候这没有后果；再拆渠道
 * 时它会重新变成一个真实的窗口。
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
                `never read a valid value, owning everything this process can consume ` +
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
