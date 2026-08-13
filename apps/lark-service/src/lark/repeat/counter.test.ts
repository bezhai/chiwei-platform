// 复读计数器的真身：一次往返，读-改-写全在 Redis 那边跑完。
//
// 这里能验的只有**一件事**，但它正是整个方案的支点：这个适配器只发一条命令。Redis 单
// 线程执行脚本，所以"一条命令"就等于"没有可交错的中间态"。开发机跑不了 Lua，脚本本身
// 的语义没法在这里执行 —— 于是分成两半：
//
//   * 本文件钉住「真身只发一条命令、参数接对了」；
//   * repeat.test.ts 钉住「只要计数器是原子的，复读就恰好发一次」，并且用一个**会交错**
//     的替身证明那条断言真的看得见并发问题。
//
// 两半合起来才是完整的论证。少了前半，"原子"只是替身里的一个假设；少了后半，串行的
// 替身会把并发问题整个掩盖掉（C4 那批吃过这个亏）。

import { describe, expect, it } from 'bun:test';

import { REPEAT_COUNTER_TTL_SECONDS, redisRepeatCounter, repeatCounterKey } from './counter';
import type { RepeatCounterRedis } from './counter';

interface Call {
    script: string;
    numKeys: number;
    args: (string | number)[];
}

function fakeRedis(answer: unknown): { redis: RepeatCounterRedis; calls: Call[] } {
    const calls: Call[] = [];
    return {
        calls,
        redis: {
            evalScript: async (script, numKeys, ...args) => {
                calls.push({ script, numKeys, args });
                return answer;
            },
        },
    };
}

describe('复读计数器的真身', () => {
    it('一次 evalScript 就完事 —— 读和写之间没有可以插进来的地方', async () => {
        const { redis, calls } = fakeRedis(3);

        const count = await redisRepeatCounter(() => redis).bump('oc_1', 'deadbeef');

        expect(count).toBe(3);
        expect(calls).toHaveLength(1);
        expect(calls[0]!.numKeys).toBe(1);
        expect(calls[0]!.args).toEqual([
            repeatCounterKey('oc_1'),
            'deadbeef',
            'oc_1',
            REPEAT_COUNTER_TTL_SECONDS,
        ]);
    });

    // 键名和存进去的 JSON 形状都是**跨服务契约**：切换窗口里 channel-server 那份复读还
    // 活着，两边读写同一个键。换了键名，同一个群会有两份互不相干的计数（复读要说六遍
    // 同样的话才出来一次）；换了字段名，对面 JSON.parse 出来的 msg 恒为 undefined，
    // 每条消息都被当成"内容变了"，复读永远不触发。
    it('键名与拆分前一字不差', () => {
        expect(repeatCounterKey('oc_1')).toBe('repeat_msg:oc_1');
    });

    it('存进去的记录仍是 {chatId, msg, repeatTime}，过期仍是 7 天', async () => {
        const { redis, calls } = fakeRedis(1);

        await redisRepeatCounter(() => redis).bump('oc_1', 'deadbeef');

        const script = calls[0]!.script;
        expect(script).toContain('chatId');
        expect(script).toContain('msg');
        expect(script).toContain('repeatTime');
        expect(REPEAT_COUNTER_TTL_SECONDS).toBe(7 * 24 * 60 * 60);
    });

    // Lua 的整数回来是 number，但 evalScript 的返回类型是 unknown，而 `"3" === 3` 是
    // false —— 复读的触发判据正是一个 `=== 3`。
    it('返回值一律收敛成数字', async () => {
        const { redis } = fakeRedis('3');

        expect(await redisRepeatCounter(() => redis).bump('oc_1', 'x')).toBe(3);
    });
});
