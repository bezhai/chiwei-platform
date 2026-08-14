// 「channel-server 的出站消费拥有哪些 channel」这份清单的解析与解析结果的时序语义。
//
// 走 Dynamic Config 而不是 env：Release env 会被 deploy 的 POST 清空，长期开关放
// 那里会在某次部署之后悄悄失效 —— 而这个开关失效的表现是「两个服务同时守着同一条
// 出站队列」，静默劈流。
//
// 「读不到就拥有全部」里的"全部"是 CHANNEL_SERVER_CHANNELS，也就是本进程有能力消费
// 的全部。它不会宽到别的服务的队列上。但**已经交出去的**不能靠一次读不到就收回来：
//
//   首次读不到（没有 last-known-good）→ 拥有全部，否则首次部署即变砖；
//   成功读到过之后再读不到          → 记得上次的结论，绝不自己变宽。
//
// DynamicConfig.get 把 fetch 失败吞成默认值（dynamic-config/index.ts::fetchSnapshot
// 里 catch 之后 return {}），所以「读失败」和「没配这个 key」在这一层是同一个信号：
// 空串。两者都按「这次没给出有效指令」处理 —— 变宽是危险方向，必须由一次显式且
// 有效的配置来驱动。
//
// 未知渠道的 fixture 用 `telegram` / `wechat`：它们从来不在清单里，所以「这不是本
// 服务的渠道」这个语义不依赖清单当下有几个渠道。

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
        // 整份都是本服务没有的渠道 = 这份配置没表达任何可执行的意图。
        expect(parseOwnedChannels('wechat,telegram')).toBeNull();
    });

    it('显式点名本服务的渠道', () => {
        expect(parseOwnedChannels('qq')).toEqual(['qq']);
    });

    it('逗号分隔、去空白、大小写不敏感、去重', () => {
        expect(parseOwnedChannels(' QQ , qq ')).toEqual(['qq']);
    });

    it('本服务没有的渠道被丢掉，但已知的照常生效', () => {
        // 拼错一个不该把整份配置作废：qq 是操作者明确写下的意图。
        expect(parseOwnedChannels('qq,wechat')).toEqual(['qq']);
    });

    // 飞书已经归 lark-service。配回来也不该生效——本服务连飞书的出站能力都没有，
    // 订上那条队列只会把 lark-service 的消息抢走一半然后在 getCapabilities 上炸。
    it('飞书不在清单里：配了也不认', () => {
        expect(parseOwnedChannels('lark')).toBeNull();
        expect(parseOwnedChannels('qq,lark')).toEqual(['qq']);
    });
});

describe('ChannelOwnership — last-known-good', () => {
    it('首次就读不到：拥有全部，否则首次部署即变砖', async () => {
        const ownership = new ChannelOwnership(async () => '');

        expect(await silently(() => ownership.resolve())).toEqual([...CHANNEL_SERVER_CHANNELS]);
    });

    it('首次读取抛异常：同样回落到拥有全部', async () => {
        const ownership = new ChannelOwnership(async () => {
            throw new Error('paas-engine unreachable');
        });

        expect(await silently(() => ownership.resolve())).toEqual([...CHANNEL_SERVER_CHANNELS]);
    });

    // 承重墙：读到过有效值之后再读不到，走的必须是 last-known-good 那条路，不是
    // bootstrap 回落。清单里只剩一个渠道时两条路的返回值恰好相同，所以这里认的是
    // **走了哪条路**（告警的 event 名），不是返回值——再拆一个渠道出去时，认返回值
    // 的断言会悄悄失效。
    it('成功读到过之后再读不到：保持上次结论，不退回 bootstrap 回落', async () => {
        let raw = 'qq';
        const ownership = new ChannelOwnership(async () => raw);
        expect(await ownership.resolve()).toEqual(['qq']);

        raw = '';
        const capture = captureLogs();
        try {
            expect(await ownership.resolve()).toEqual(['qq']);
        } finally {
            capture.restore();
        }

        const joined = capture.lines.join('\n');
        expect(joined).toContain('outbound_channels_config_unavailable');
        expect(joined).not.toContain('outbound_channels_bootstrap_default');
    });

    it('成功读到过之后读取抛异常：同样保持上次结论', async () => {
        let fail = false;
        const ownership = new ChannelOwnership(async () => {
            if (fail) throw new Error('paas-engine unreachable');
            return 'qq';
        });
        expect(await ownership.resolve()).toEqual(['qq']);

        fail = true;
        expect(await silently(() => ownership.resolve())).toEqual(['qq']);
    });

    it('成功读到过之后配置被填成一堆未知渠道：保持上次结论', async () => {
        let raw = 'qq';
        const ownership = new ChannelOwnership(async () => raw);
        await ownership.resolve();

        raw = 'wechat,telegram';
        expect(await silently(() => ownership.resolve())).toEqual(['qq']);
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

    it('首次回落到拥有全部时也告警', async () => {
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
    it('飞书移交之后 channel-server 的出站只剩 qq', () => {
        expect(CHANNEL_SERVER_CHANNELS).toEqual(['qq']);
    });

    it('开关 key 稳定（Dynamic Config 里按它配）', () => {
        expect(OUTBOUND_CHANNELS_KEY).toBe('channel_server_outbound_channels');
    });
});
