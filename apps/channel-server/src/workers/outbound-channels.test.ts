// 「channel-server 的出站消费拥有哪些 channel」这份清单的解析与解析结果的时序语义。
//
// 走 Dynamic Config 而不是 env：Release env 会被 deploy 的 POST 清空，长期开关放
// 那里会在某次部署之后悄悄失效 —— 而这个开关失效的表现是「channel-server 又开始
// 抢 lark-service 的消息」，静默劈流。
//
// 但「读不到就拥有全部」只在**移交之前**是对的。lark 交出去之后，一次 Dynamic
// Config 瞬断就会让这个 worker 重新抢 lark 的队列，RabbitMQ 轮询把流量随机劈成
// 两半，不报错不留痕 —— 正是决策八要根治的那个故障。所以：
//
//   首次读不到（没有 last-known-good）→ 保持现状行为（拥有全部），否则首次部署即变砖；
//   成功读到过之后再读不到          → 记得上次的结论，绝不自己变宽。
//
// DynamicConfig.get 把 fetch 失败吞成默认值（dynamic-config/index.ts::fetchSnapshot
// 里 catch 之后 return {}），所以「读失败」和「没配这个 key」在这一层是同一个信号：
// 空串。两者都按「这次没给出有效指令」处理 —— 变宽是危险方向，必须由一次显式且
// 有效的配置来驱动。

import { describe, it, expect } from 'bun:test';

import {
    CHANNEL_SERVER_CHANNELS,
    ChannelOwnership,
    OUTBOUND_CHANNELS_KEY,
    parseOwnedChannels,
} from './outbound-channels';

/** 收集 console.warn / console.error，断言告警确实发出（可以用 make logs 捞到）。 */
function captureLogs(): { lines: string[]; restore: () => void } {
    const lines: string[] = [];
    const warn = console.warn;
    const error = console.error;
    const sink =
        (level: string) =>
        (...args: unknown[]): void => {
            lines.push(`${level} ${args.map((a) => String(a)).join(' ')}`);
        };
    console.warn = sink('warn');
    console.error = sink('error');
    return {
        lines,
        restore: () => {
            console.warn = warn;
            console.error = error;
        },
    };
}

async function silently<T>(fn: () => Promise<T>): Promise<T> {
    const capture = captureLogs();
    try {
        return await fn();
    } finally {
        capture.restore();
    }
}

describe('parseOwnedChannels — 只负责解析，不负责兜底', () => {
    it('没有有效指令时返回 null（兜底是调用方的时序决策，不是解析器的）', () => {
        expect(parseOwnedChannels('')).toBeNull();
        expect(parseOwnedChannels('   ')).toBeNull();
        // 整份都是未知渠道 = 这份配置没表达任何可执行的意图。
        expect(parseOwnedChannels('wechat,line')).toBeNull();
    });

    it('显式收窄到单个渠道', () => {
        expect(parseOwnedChannels('qq')).toEqual(['qq']);
    });

    it('逗号分隔、去空白、大小写不敏感、去重', () => {
        expect(parseOwnedChannels(' QQ , lark , qq ')).toEqual(['qq', 'lark']);
    });

    it('未知渠道被丢掉，但已知的照常生效', () => {
        // 拼错一个不该把整份配置作废：qq 是操作者明确写下的意图。
        expect(parseOwnedChannels('qq,wechat')).toEqual(['qq']);
    });
});

describe('ChannelOwnership — last-known-good', () => {
    it('首次就读不到：保持现状行为（拥有全部），否则首次部署即变砖', async () => {
        const ownership = new ChannelOwnership(async () => '');

        expect(await silently(() => ownership.resolve())).toEqual([...CHANNEL_SERVER_CHANNELS]);
    });

    it('首次读取抛异常：同样回落到拥有全部', async () => {
        const ownership = new ChannelOwnership(async () => {
            throw new Error('paas-engine unreachable');
        });

        expect(await silently(() => ownership.resolve())).toEqual([...CHANNEL_SERVER_CHANNELS]);
    });

    // 这条是本次修复的承重墙：lark 已经移交给 lark-service 之后，配置瞬断绝不能让
    // 这个 worker 重新认领 lark —— 那会让两个服务同时消费 lark 的出站队列。
    it('成功收窄过之后再读不到：保持上次结论，绝不自己变宽', async () => {
        let raw = 'qq';
        const ownership = new ChannelOwnership(async () => raw);
        expect(await ownership.resolve()).toEqual(['qq']);

        raw = '';
        expect(await silently(() => ownership.resolve())).toEqual(['qq']);
    });

    it('成功收窄过之后读取抛异常：同样保持上次结论', async () => {
        let fail = false;
        const ownership = new ChannelOwnership(async () => {
            if (fail) throw new Error('paas-engine unreachable');
            return 'qq';
        });
        expect(await ownership.resolve()).toEqual(['qq']);

        fail = true;
        expect(await silently(() => ownership.resolve())).toEqual(['qq']);
    });

    it('成功收窄过之后配置被填成一堆未知渠道：保持上次结论', async () => {
        let raw = 'qq';
        const ownership = new ChannelOwnership(async () => raw);
        await ownership.resolve();

        raw = 'wechat,line';
        expect(await silently(() => ownership.resolve())).toEqual(['qq']);
    });

    it('变宽要靠一次显式且有效的配置（回滚路径照常可用）', async () => {
        let raw = 'qq';
        const ownership = new ChannelOwnership(async () => raw);
        await ownership.resolve();

        raw = 'qq,lark';
        expect(await ownership.resolve()).toEqual(['qq', 'lark']);
    });

    it('保持上次结论时带稳定 event 名告警，不静默', async () => {
        let raw = 'qq';
        const ownership = new ChannelOwnership(async () => raw);
        await ownership.resolve();

        raw = '';
        const capture = captureLogs();
        try {
            await ownership.resolve();
        } finally {
            capture.restore();
        }

        const joined = capture.lines.join('\n');
        expect(joined).toContain('outbound_channels_config_unavailable');
        // 告警里要能看出「我现在守着哪些 channel」，否则排查时还得去翻配置。
        expect(joined).toContain('qq');
    });

    it('首次回落到拥有全部时也告警（这是 cutover 期最危险的一次回落）', async () => {
        const ownership = new ChannelOwnership(async () => '');
        const capture = captureLogs();
        try {
            await ownership.resolve();
        } finally {
            capture.restore();
        }

        expect(capture.lines.join('\n')).toContain('outbound_channels_bootstrap_default');
    });
});

describe('清单本身', () => {
    it('cutover 窗口里 channel-server 同时拥有 lark 和 qq', () => {
        // Task F 关闭窗口时 lark 从这里删掉 —— 那一刻起 bootstrap 回落本身也安全了。
        expect(CHANNEL_SERVER_CHANNELS).toEqual(['lark', 'qq']);
    });

    it('开关 key 稳定（Dynamic Config 里按它配）', () => {
        expect(OUTBOUND_CHANNELS_KEY).toBe('channel_server_outbound_channels');
    });
});
