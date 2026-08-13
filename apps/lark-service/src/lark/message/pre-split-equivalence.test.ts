// 拆分前后行为等价的判据。
//
// `pre-split-parser-output.json` 里的每一条都是 **channel-server 那两套解析器真
// 实跑出来的输出**：38 个样本各喂给 `MessageTransferer`（飞书原生形态）和
// `larkInbound.parse`（通用契约）各跑一遍，把结果原样落盘。本文件拿同一批样本喂
// 给合并后的解析器，逐条对。
//
// 为什么要有这份数据而不是只靠上面几个单元测试：单元测试断言的是"我认为应该是
// 什么"，这份数据断言的是"拆分前实际上是什么"。等 channel-server 那份飞书代码删
// 掉之后，这是唯一还能回答后一个问题的东西。
//
// 数据是怎么来的：在 channel-server 里临时挂一个 harness，同时 import 两套旧解析
// 器和本服务的新解析器，跑完把旧输出写进这个文件，然后把 harness 删掉。要重新生
// 成就照这个办法再来一次 —— 但注意 Task F 之后旧解析器就不存在了，这份数据从那
// 时起只能是历史快照。
//
// **两处刻意的分歧**见下方 DELIBERATE_DIVERGENCES：都是旧的两套解析器**彼此不一
// 致**的地方，合并时只能二选一。两处都选了通用契约那一侧的做法，所以合并后的解析
// 器与旧的通用契约解析器**逐字节一致**（38/38），只跟旧的飞书原生解析器在这两条上
// 不同。

import { describe, expect, it } from 'bun:test';

import { readLarkMessageEvent } from './read-message-event';
import type { LarkBotLookup } from './mentions';
import type { LarkMessageEvent } from './wire';
import corpus from './pre-split-parser-output.json';

// harness 里给两侧喂的是同一组 bot：一个我们自己在跑、但没绑人设的 bot。
// 绑了人设的分支两侧都是"查到就用人设名"，形状完全一样，由 mentions.test.ts 覆盖。
const bots: LarkBotLookup = {
    byAppId: (appId) =>
        appId === 'cli_registered'
            ? { botName: 'chiwei', displayName: null, commonUserId: 'cu_chiwei' }
            : null,
    byUnionId: (unionId) =>
        unionId === 'on_registered_bot'
            ? { botName: 'chiwei', displayName: null, commonUserId: 'cu_chiwei' }
            : null,
};

interface Divergence {
    why: string;
    larkParts?: unknown;
    larkMentions?: unknown;
}

const DELIBERATE_DIVERGENCES: Record<string, Divergence> = {
    // 旧的飞书原生解析器按**数组下标**取 mention（`mentions[N-1]`），旧的通用契约
    // 解析器按 **key** 取（`byKey.get('@_user_N')`）。飞书自己的约定是"正文里的占位
    // 符就是 mention 记录的 key"，下标只是"key 恰好按顺序排"时才成立的巧合。合并后
    // 一律按 key —— 于是这个样本（mentions 故意乱序）下，新的飞书原生形态跟旧的不
    // 同，但跟旧的通用契约一致。
    'text/mention-list-out-of-order': {
        why: 'mention 按 key 取，不按数组下标取',
        larkParts: [
            { type: 'mention', value: '张三', meta: { channel_user_id: 'on_a' } },
            { type: 'text', value: ' and ' },
            { type: 'mention', value: '李四', meta: { channel_user_id: 'on_b' } },
        ],
    },
    // 飞书没给名字时，旧的飞书原生解析器留空串（渲染成一个光秃秃的 "@"，读的人根本
    // 不知道被 @ 的是谁），旧的通用契约解析器退到 union_id。合并后一律退到 id。
    'text/nameless-mention': {
        why: '名字缺失时退到 id，不留空串',
        larkParts: [
            { type: 'mention', value: 'on_c', meta: { channel_user_id: 'on_c' } },
            { type: 'text', value: ' hi' },
        ],
        larkMentions: [{ id: 'on_c', displayName: 'on_c' }],
    },
};

/**
 * 跟落盘时同一个口径：undefined 的字段视作不存在。
 *
 * 返回 unknown 是刻意的 —— 对面是从 JSON 读进来的数据，类型被放宽成了 string，
 * 硬对齐两边的静态类型只会逼出一堆 as，而这里要比的本来就是运行时的值。
 */
function plain(value: unknown): unknown {
    return JSON.parse(JSON.stringify(value));
}

describe('pre-split parser output', () => {
    it('covers every message type the old parsers knew about', () => {
        const types = new Set(corpus.map((s) => s.event.message.message_type));
        expect([...types].sort()).toEqual([
            'audio',
            'file',
            'image',
            'media',
            'merge_forward',
            'post',
            'share_chat',
            'share_user',
            'sticker',
            'text',
            'todo', // 未知类型
        ]);
        expect(corpus.length).toBe(38);
    });

    // 分歧清单只能缩不能长。多出一条就意味着有人在改解析行为，必须先解释清楚。
    it('admits exactly two deliberate divergences from the pre-split behaviour', () => {
        expect(Object.keys(DELIBERATE_DIVERGENCES).sort()).toEqual([
            'text/mention-list-out-of-order',
            'text/nameless-mention',
        ]);
    });

    describe.each(corpus.map((s) => [s.name, s] as const))('%s', (name, sample) => {
        const reading = readLarkMessageEvent(sample.event as LarkMessageEvent, bots)!;
        const divergence = DELIBERATE_DIVERGENCES[name];

        it('produces the pre-split generic contract byte for byte', () => {
            expect(plain(reading.inbound)).toEqual(sample.contract as unknown);
        });

        it('produces the pre-split Lark-native parts', () => {
            expect(plain(reading.content)).toEqual(
                (divergence?.larkParts ?? sample.larkParts) as unknown,
            );
        });

        it('produces the pre-split Lark-native mention list', () => {
            const actual = reading.mentions.all.map((m) => ({
                id: m.unionId,
                displayName: m.displayName,
                botCommonUserId: m.botCommonUserId,
            }));
            expect(plain(actual)).toEqual(
                (divergence?.larkMentions ?? sample.larkMentions) as unknown,
            );
        });
    });
});
