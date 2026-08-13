// channel-server 的**入站**信封消费拥有哪些 channel。
//
// 分区队列本身不解决竞争消费：`inbound_lane.lark.{lane}` 只有一个 owner 这件事，得
// 有人真的不去订阅它。而 channel-server 在 cutover 窗口内仍然注册着 lark runtime
// （代码删不掉，删了就只能靠回滚镜像，决策七），所以"订阅面跟着 runtime 走"等于两个
// 服务在整个窗口里一直抢同一条队列——RabbitMQ 轮询把飞书流量随机劈成两半，两边都真
// 的处理，不报错、不留痕。
//
// 所以移交必须是一个能在运行期做的显式动作，而不是"等 Task F 删代码自然收窄"。
//
// ## 为什么不复用出站那个 key
//
// 出站有 `channel_server_outbound_channels`（workers/outbound-channels.ts），语义是
// 一样的"我拥有哪些 channel"，但**移交时刻不一样**：决策九要求三条队列各自走一遍
// 「双订阅 → 切生产者 → 排空 → drain 屏障移交」，一条一条来，每一步都要能单独回退。
// 共用一个 key 等于把入站和出站两次移交焊死成一次——出站出了问题想退回来，入站会
// 跟着一起退，而入站那条链路可能已经切干净了。两个 key 才能表达"入站已移交、出站还
// 没"这个必然出现的中间态。
//
// 走 Dynamic Config 而不是 env：Release env 会被 deploy 的 POST 清空，长期开关放那
// 里会在某次部署之后悄悄失效，而这个开关失效的表现就是上面那句"两个服务又开始抢"。
//
// ## 移交飞书入站的顺序
//
// 1. lark-service 先在那条泳道上跑起来（它默认就订着分区前的共享队列）。**先摘所有权
//    的话，飞书的信封会一直在共享队列上弹，没人接**——不丢（这条队列刻意不配 TTL /
//    死信），但一直堆着。
// 2. 把本 key 收窄成 `qq`。从这一刻起本服务在**两种队列上**都不再认领飞书的信封，
//    逐条现读，不用重启。
// 3. 再翻 enable_inbound_lane_channel_consume（订阅是启动动作，要重启），这时本服务
//    只会订上 inbound_lane.qq.{lane}，和 lark-service 的订阅面不再相交。
// 4. 最后才切生产者（enable_inbound_lane_channel_publish）。

import { DynamicConfig } from '@inner/shared';

export const INBOUND_LANE_CHANNELS_KEY = 'channel_server_inbound_channels';

// ⚠️ 读它的地方没有请求上下文（消费者在 context.run 之前就要判归属），所以 Dynamic
// Config 按 **prod** 解析 —— 跟同一套的两个 flag 一样，这是个全局开关，给某条泳道单独
// 配不会生效。代价要认：摘掉 lark 会让**所有**跑着 channel-server 的泳道一起不再认领
// 飞书信封，而只有部署了 lark-service 的那条泳道有人接手，其余泳道的飞书信封会一直在
// 队列里弹（不丢，但没人处理）。所以收窄的时机是"这一批泳道都已经有 lark-service"。
const dynamicConfig = new DynamicConfig();

/**
 * 解析配置值，**不做兜底**。
 *
 * `handled` 是本进程注册了入站信封处理能力的 channel（由 plugins 的 runtime 注册面
 * 算出）。配置只能在它之内收窄——配了本进程压根处理不了的 channel，订阅上去也只会
 * 一路 requeue。
 *
 * 返回 null =「这份配置没给出有效指令」（没配 / 空 / 全填错）。兜底成什么取决于之前
 * 有没有读到过有效值，那是时序问题，归 InboundChannelOwnership 管。
 */
export function parseInboundLaneChannels(raw: string, handled: string[]): string[] | null {
    const known = new Set(handled);
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
            `[inbound-lane] ${INBOUND_LANE_CHANNELS_KEY} names channels this process has no ` +
                `inbound runtime for [${unknown.join(', ')}]; it can handle [${handled.join(', ')}]`,
        );
    }
    return owned.length > 0 ? owned : null;
}

/**
 * 「我拥有哪些 channel」的权威解析，带 last-known-good。
 *
 * 「读不到就拥有全部」只在**移交之前**成立。飞书交给 lark-service 之后，一次 Dynamic
 * Config 瞬断（或者一次重启撞上瞬断）就会让本服务重新认领飞书的信封——而 cutover 窗口
 * 里 lark runtime 还注册着，抢到就**真的处理掉**，不报错、不留痕。出站那边抢错了至少
 * 还有 fail-closed 的 nack 留下信号，入站这条路一点声音都没有。所以时序上分两种：
 *
 * - **从没成功读到过**：没有可依据的结论，保持现状行为（拥有全部能处理的）。首次部署
 *   时配置还没建、或者操作者压根没打算收窄，回落到空集等于消费者变砖、信封静默堆积。
 * - **成功读到过之后再读不到**：记得上次的结论，绝不自己变宽。变宽是危险方向，必须由
 *   一次显式且有效的配置来驱动 —— 把 channel 加回来照常有效（回滚路径）。
 *
 * DynamicConfig.get 把 fetch 失败吞成默认值（dynamic-config/index.ts::fetchSnapshot
 * catch 之后 return {}），所以「读失败」和「没配这个 key」在这层是同一个信号：空串。
 * read() 自己抛异常也按同一条路处理。三者的共同点是「这次没拿到有效指令」，而对「不许
 * 自己变宽」这条不变量来说，处置完全一样。
 *
 * 残留风险，写在这里免得下次有人以为它被解决了：last-known-good 只活在进程内存里。
 * 进程重启 + 恰好那一刻 Dynamic Config 读不到，就会退回 bootstrap 回落（拥有全部），
 * 而订阅面是启动时定的——那一轮会连 inbound_lane.lark.{lane} 一起订上。真正关掉这个
 * 窗口的是 Task F：lark runtime 删掉之后 handled 里没有 lark，回落再宽也宽不到它上面。
 */
export class InboundChannelOwnership {
    private lastGood: string[] | null = null;

    constructor(private readonly read: () => Promise<string>) {}

    async resolve(handled: string[]): Promise<string[]> {
        let parsed: string[] | null = null;
        let failure: string | null = null;
        try {
            parsed = parseInboundLaneChannels(await this.read(), handled);
        } catch (err) {
            failure = (err as Error).message;
        }

        if (parsed !== null) {
            this.lastGood = parsed;
            return [...parsed];
        }

        const why = failure ?? `${INBOUND_LANE_CHANNELS_KEY} is unset or names no known channel`;
        if (this.lastGood !== null) {
            // 稳定 event 名，make logs KEYWORD=inbound_channels_config_unavailable 可捞。
            console.error(
                `[inbound-lane] inbound_channels_config_unavailable: ${why}; ` +
                    `keeping last known good [${this.lastGood.join(', ')}] — widening back ` +
                    `would start claiming envelopes that were already handed off`,
            );
            return [...this.lastGood];
        }

        console.warn(
            `[inbound-lane] inbound_channels_bootstrap_default: ${why}; ` +
                `never read a valid value, owning all of [${handled.join(', ')}]`,
        );
        return [...handled];
    }
}

// 进程级单例：last-known-good 要跨"逐条现读"存活，每次新建一个就等于没有记忆。
const ownership = new InboundChannelOwnership(() =>
    dynamicConfig.get(INBOUND_LANE_CHANNELS_KEY, ''),
);

export function loadInboundLaneChannels(handled: string[]): Promise<string[]> {
    return ownership.resolve(handled);
}
