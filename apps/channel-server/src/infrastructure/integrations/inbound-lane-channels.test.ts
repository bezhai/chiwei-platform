// 入站所有权收窄的解析语义。
//
// 这个 key 决定移交进行中某个 channel 的入站信封归谁：配错的后果是两个服务继续抢同
// 一条队列（不报错、不留痕），或者本服务连自己的 channel 都不收（队列静默堆积）。所以
// 每条兜底都要有断言，不能只测 happy path。飞书的入站就是靠它交给 lark-service 的。

import { describe, expect, it } from 'bun:test';

import {
    INBOUND_LANE_CHANNELS_KEY,
    InboundChannelOwnership,
    parseInboundLaneChannels,
} from './inbound-lane-channels';

const HANDLED = ['lark', 'qq'];

describe('INBOUND_LANE_CHANNELS_KEY', () => {
    // ⚠️ 出站是另一个 key（workers/outbound-channels.ts 的 channel_server_outbound_channels）。
    // 入站和出站各自走一遍移交，共用一个 key 就没法表达"入站已移交、出站还没"这个必然
    // 出现的中间态。
    it('is not the outbound ownership key', () => {
        expect(INBOUND_LANE_CHANNELS_KEY).toBe('channel_server_inbound_channels');
        expect(INBOUND_LANE_CHANNELS_KEY).not.toBe('channel_server_outbound_channels');
    });
});

describe('parseInboundLaneChannels', () => {
    it('narrows to exactly what the operator named', () => {
        expect(parseInboundLaneChannels('qq', HANDLED)).toEqual(['qq']);
    });

    it('takes a comma separated list, whitespace and case included', () => {
        expect(parseInboundLaneChannels(' QQ , lark ', HANDLED)).toEqual(['qq', 'lark']);
    });

    it('says each channel once', () => {
        expect(parseInboundLaneChannels('qq,qq', HANDLED)).toEqual(['qq']);
    });

    // 兜底成什么是时序问题（之前有没有读到过有效值），归 InboundChannelOwnership 管。
    // 解析这一层只回答"这份配置有没有给出有效指令"。
    it('reports no instruction when nothing is configured', () => {
        expect(parseInboundLaneChannels('', HANDLED)).toBeNull();
    });

    it('reports no instruction when the config names nothing it can handle', () => {
        expect(parseInboundLaneChannels('slack', HANDLED)).toBeNull();
    });

    // 本进程没有那个 channel 的入站 runtime，订上去只会一路 requeue。
    it('drops channels this process has no inbound runtime for', () => {
        expect(parseInboundLaneChannels('lark,qq', ['qq'])).toEqual(['qq']);
    });
});

// 「读不到就拥有全部」的"全部"是 handled，宽不到本进程处理不了的 channel 上。但移交
// 进行中（对方已经接手、本进程的 runtime 还在）不一样：一次 Dynamic Config 瞬断就会让
// 本服务重新认领已经交出去的信封，抢到就**真的处理掉**，不报错、不留痕，切流永远做不
// 干净。
describe('InboundChannelOwnership', () => {
    it('owns everything it can handle before it has ever read a valid value', async () => {
        const ownership = new InboundChannelOwnership(async () => '');
        expect(await ownership.resolve(HANDLED)).toEqual(HANDLED);
    });

    it('narrows to what a valid value names', async () => {
        const ownership = new InboundChannelOwnership(async () => 'qq');
        expect(await ownership.resolve(HANDLED)).toEqual(['qq']);
    });

    it('keeps the last good answer when the config goes away', async () => {
        let raw = 'qq';
        const ownership = new InboundChannelOwnership(async () => raw);

        expect(await ownership.resolve(HANDLED)).toEqual(['qq']);
        raw = '';

        expect(await ownership.resolve(HANDLED)).toEqual(['qq']);
    });

    // DynamicConfig 把 fetch 失败吞成默认值，所以"读失败"通常长得跟"没配"一样；但
    // read() 自己抛出来的时候处置必须相同 —— 都是"这次没拿到有效指令"。
    it('keeps the last good answer when the read throws', async () => {
        let boom = false;
        const ownership = new InboundChannelOwnership(async () => {
            if (boom) throw new Error('paas-engine is unreachable');
            return 'qq';
        });

        expect(await ownership.resolve(HANDLED)).toEqual(['qq']);
        boom = true;

        expect(await ownership.resolve(HANDLED)).toEqual(['qq']);
    });

    it('keeps the last good answer when the config names nothing it can handle', async () => {
        let raw = 'qq';
        const ownership = new InboundChannelOwnership(async () => raw);

        await ownership.resolve(HANDLED);
        raw = 'slack';

        expect(await ownership.resolve(HANDLED)).toEqual(['qq']);
    });

    // 变宽是危险方向，但必须做得到：把 channel 加回来是回滚路径。
    it('widens again when a valid config says so', async () => {
        let raw = 'qq';
        const ownership = new InboundChannelOwnership(async () => raw);

        await ownership.resolve(HANDLED);
        raw = 'lark,qq';

        expect(await ownership.resolve(HANDLED)).toEqual(['lark', 'qq']);
    });

    // 交出去的是一份拷贝：调用方（消费者逐条判归属）改了它不该动到记忆，否则一次
    // 误改会在下一次配置读不到时变成"我拥有的 channel 变多了"。
    it('never hands out the array it remembers', async () => {
        let raw = 'qq';
        const ownership = new InboundChannelOwnership(async () => raw);

        const first = await ownership.resolve(HANDLED);
        first.push('lark');
        raw = ''; // 配置读不到，只能靠记忆

        expect(await ownership.resolve(HANDLED)).toEqual(['qq']);
    });

    it('never hands out the caller list it was given', async () => {
        const handled = ['lark', 'qq'];
        const ownership = new InboundChannelOwnership(async () => '');

        (await ownership.resolve(handled)).push('slack');

        expect(handled).toEqual(['lark', 'qq']);
    });
});
