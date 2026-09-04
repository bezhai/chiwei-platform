// 真人在飞书撤回一条消息之后，公共层那一行要被标上撤回时刻 —— 否则赤尾读会话时
// 照样看得到它，还可能接一句对面已经看不到的话。
//
// 这条链没有任何重投：入口先应答、处理跑在没人跟踪的 Promise 里，进程收到停止信号
// 就直接退出。所以每一种失败都必须自己走到一个终态并留下日志，不能靠"下次再来一遍"。

import { describe, expect, it, spyOn } from 'bun:test';

import { receiveLarkRecall, type LarkRecallDeps } from './recall-message';
import type { LarkMessageRow } from './projection/tables';

const OM_ID = 'om_recalled';
const COMMON_MESSAGE_ID = 'cm_recalled';

/** 入口应答那一刻。落库的撤回时刻永远是它 —— 报文给的 recall_time 一概不解析。 */
const RECEIVED_AT = new Date('2026-09-04T06:50:54.000Z');

function larkMessageRow(): LarkMessageRow {
    return {
        om_id: OM_ID,
        common_message_id: COMMON_MESSAGE_ID,
        chat_id: 'oc_1',
        message_type: 'text',
    };
}

interface Marked {
    commonMessageId: string;
    recalledAt: Date;
}

interface Harness {
    deps: LarkRecallDeps;
    /** markCommonMessageRecalled 收到的每一次调用，含被首写保留挡下的那些。 */
    attempts: Marked[];
    /** 真的写进去的那一条（首写保留：只可能有一条）。 */
    written: Marked | undefined;
}

function harness(
    options: {
        mapping?: LarkMessageRow | null;
        lookupFails?: Error;
        writeFails?: Error;
    } = {},
): Harness {
    const attempts: Marked[] = [];
    const h: Harness = {
        attempts,
        written: undefined,
        deps: {
            store: {
                async larkMessage(omId) {
                    if (options.lookupFails) throw options.lookupFails;
                    if (options.mapping !== undefined) return options.mapping;
                    return omId === OM_ID ? larkMessageRow() : null;
                },
                async markCommonMessageRecalled(commonMessageId, recalledAt) {
                    if (options.writeFails) throw options.writeFails;
                    attempts.push({ commonMessageId, recalledAt });
                    // 真身那条 UPDATE 带着 `recalled_at IS NULL`，所以首写之后一律 0 行。
                    if (h.written) return false;
                    h.written = { commonMessageId, recalledAt };
                    return true;
                },
            },
        },
    };
    return h;
}

/** 一次真实形状的撤回报文：全部字段可选，撤回者身份根本不在里面。 */
function recallEvent(overrides: Record<string, unknown> = {}) {
    return {
        message_id: OM_ID,
        chat_id: 'oc_1',
        recall_time: '1757573454',
        recall_type: 'message_owner' as const,
        ...overrides,
    };
}

/** 把一段处理跑完，同时把它说的话收起来。 */
async function saying(run: () => Promise<void>) {
    const info = spyOn(console, 'info').mockImplementation(() => {});
    const warn = spyOn(console, 'warn').mockImplementation(() => {});
    const error = spyOn(console, 'error').mockImplementation(() => {});
    try {
        await run();
        return {
            info: info.mock.calls.flat().map(String).join(' '),
            warn: warn.mock.calls.flat().map(String).join(' '),
            error: error.mock.calls.flat().map(String).join(' '),
        };
    } finally {
        info.mockRestore();
        warn.mockRestore();
        error.mockRestore();
    }
}

describe('receiveLarkRecall', () => {
    it('把公共层对应那一行标上撤回时刻', async () => {
        const h = harness();
        await saying(() => receiveLarkRecall(h.deps, recallEvent(), RECEIVED_AT));

        expect(h.written).toEqual({
            commonMessageId: COMMON_MESSAGE_ID,
            recalledAt: RECEIVED_AT,
        });
    });

    // recall_time 的单位仓里没有实证样本能裁决：飞书文档的示例是 13 位（毫秒形态），
    // 按秒解析它会得到几万年后的时刻，被因果检查退回收到时刻 —— 看着"没事"，实际是把
    // 飞书给的时刻整个丢掉且无人察觉。所以不解析它的数值：撤回时刻一律用收到事件那一刻。
    //
    // 这里第一条（10 位）是防回归的那条：真要有人把按秒解析加回来，只有它会红。
    describe('撤回时刻：一律是收到事件那一刻，不解析 recall_time', () => {
        const shapes: Array<[string, string | undefined]> = [
            ['10 位（秒形态）', '1757573454'],
            ['13 位（毫秒形态，飞书文档示例的形状）', '1615380573411'],
            ['报文没给', undefined],
            ['不是数字', 'just now'],
            ['零', '0'],
            ['负数', '-1'],
        ];

        for (const [shape, recall_time] of shapes) {
            it(`recall_time 是${shape}时，落库的还是收到事件那一刻`, async () => {
                const h = harness();
                await saying(() =>
                    receiveLarkRecall(h.deps, recallEvent({ recall_time }), RECEIVED_AT),
                );
                expect(h.written!.recalledAt).toEqual(RECEIVED_AT);
            });
        }
    });

    // 落库的不是飞书给的值，那飞书给的值就必须留在别处 —— 否则 prod 上永远拿不到
    // 判定单位所需的第一个实证样本。原样进成功日志，垃圾值也照记。
    describe('成功日志留下 recall_time 的原始值', () => {
        const rawValues = ['1757573454', '1615380573411', 'just now'];

        for (const recall_time of rawValues) {
            it(`recall_time=${recall_time} 原样出现在成功日志里`, async () => {
                const h = harness();
                const said = await saying(() =>
                    receiveLarkRecall(h.deps, recallEvent({ recall_time }), RECEIVED_AT),
                );
                expect(said.info).toContain(recall_time);
            });
        }

        it('报文没给 recall_time 时成功日志照样落地，不因缺字段少记一条', async () => {
            const h = harness();
            const said = await saying(() =>
                receiveLarkRecall(h.deps, recallEvent({ recall_time: undefined }), RECEIVED_AT),
            );
            expect(said.info).toContain(COMMON_MESSAGE_ID);
            expect(said.info).toContain(OM_ID);
        });
    });

    describe('失败分支：每一种都自己走到终态，不抛出去', () => {
        // 没有消息标识就无从定位。抛出去也没人接得住 —— 应答早发出去了。
        it('报文没有消息标识时不抛错，日志里带着会话标识', async () => {
            const h = harness();
            const said = await saying(() =>
                receiveLarkRecall(h.deps, recallEvent({ message_id: undefined }), RECEIVED_AT),
            );

            expect(h.attempts).toEqual([]);
            expect(said.warn).toContain('oc_1');
        });

        // 两种原因分不开：撤回事件跟原消息的投影并发（重试能救），或者那条消息压根
        // 没落库（重试无用）。一次尝试、不重试，放弃时留下带消息标识的日志。
        it('定位不到那条消息时不抛错，日志里带着消息标识', async () => {
            const h = harness({ mapping: null });
            const said = await saying(() =>
                receiveLarkRecall(h.deps, recallEvent(), RECEIVED_AT),
            );

            expect(h.attempts).toEqual([]);
            expect(said.warn).toContain(OM_ID);
            expect(said.error).toBe('');
        });

        // 「定位不到」和「库炸了」原因完全不同：前者是结论，后者是这一跳没跑成。
        // 混在一条日志里，排查时分不出该去查投影还是去查数据库。
        it('读库报错跟定位不到分开记，而且不抛出去', async () => {
            const h = harness({ lookupFails: new Error('connection terminated') });
            const said = await saying(() =>
                receiveLarkRecall(h.deps, recallEvent(), RECEIVED_AT),
            );

            expect(said.error).toContain(OM_ID);
            expect(said.error).toContain('connection terminated');
            expect(said.warn).toBe('');
        });

        it('写库报错也不抛出去，同样单独记一条', async () => {
            const h = harness({ writeFails: new Error('deadlock detected') });
            const said = await saying(() =>
                receiveLarkRecall(h.deps, recallEvent(), RECEIVED_AT),
            );

            expect(said.error).toContain('deadlock detected');
            expect(said.warn).toBe('');
        });
    });

    // 飞书不保证只推一次，而且这条链上没有任何幂等键。首写保留让重复到达的结果一致。
    describe('重复事件', () => {
        it('同一条消息的撤回事件到两次，撤回时刻还是第一次收到它的那一刻', async () => {
            const h = harness();
            await saying(() =>
                receiveLarkRecall(h.deps, recallEvent({ recall_time: '1757573454' }), RECEIVED_AT),
            );
            await saying(() =>
                receiveLarkRecall(
                    h.deps,
                    recallEvent({ recall_time: '1757573999' }),
                    new Date('2026-09-04T07:00:00.000Z'),
                ),
            );

            expect(h.attempts).toHaveLength(2);
            expect(h.written!.recalledAt).toEqual(RECEIVED_AT);
        });

        it('第二次被首写保留挡下时不抛错', async () => {
            const h = harness();
            await saying(() => receiveLarkRecall(h.deps, recallEvent(), RECEIVED_AT));
            const said = await saying(() =>
                receiveLarkRecall(h.deps, recallEvent(), RECEIVED_AT),
            );

            expect(said.error).toBe('');
            expect(said.warn).toBe('');
            expect(said.info).toContain(COMMON_MESSAGE_ID);
        });
    });
});
