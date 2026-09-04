// 装配层的断言：这个进程到底订哪几条队列。
//
// 为什么这件事要单独钉住：撤回和回复是两条独立的链，各自的 binding 各在各的文件里，
// 各自的单测也各自绿。把其中一条从装配里删掉 —— 比如整条撤回链路 —— 之前**一个测试
// 都不会红**，而它的表现是"赤尾说错话之后没人来撤"，队列在涨、进程健康、日志干净。
//
// 队列名不写字面量，接到 contracts/mq-channel-routes.json 那份跨语言契约向量上：
// 生产者在 Python 那边，两边各写各的 expected 时"改实现顺手改 expected"会同时变绿。

import { describe, expect, it } from 'bun:test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ConsumeMessage } from 'amqplib';

import type { LarkChatResponse } from './chat-response';
import { larkOutboundQueues, type LarkOutboundQueueDeps } from './queues';
import type { LarkRecallOutcome, LarkRecallPayload, LarkRecallRequest } from './recall';

interface ContractCase {
    base: string;
    channel: string;
    lane: string | null;
    expect: { queue: string; rk: string };
}

const CONTRACT_PATH = resolve(import.meta.dir, '../../../../../contracts/mq-channel-routes.json');
const contract = JSON.parse(readFileSync(CONTRACT_PATH, 'utf8')) as { cases: ContractCase[] };

/** 契约里飞书在 prod 的那两条出站队列。 */
function contractQueue(base: string): string {
    const found = contract.cases.find(
        (c) => c.base === base && c.channel === 'lark' && c.lane === null,
    );
    if (!found) throw new Error(`contract has no lark/prod case for ${base}`);
    return found.expect.queue;
}

interface Assembled {
    queues: ReturnType<typeof larkOutboundQueues>;
    delivered: LarkChatResponse[];
    recalled: LarkRecallRequest[];
    acked: string[];
    nacked: string[];
}

function assemble(): Assembled {
    const delivered: LarkChatResponse[] = [];
    const recalled: LarkRecallRequest[] = [];
    const acked: string[] = [];
    const nacked: string[] = [];

    const deps: LarkOutboundQueueDeps = {
        amqp: {
            ack: (msg) => void acked.push(idOf(msg)),
            nack: (msg) => void nacked.push(idOf(msg)),
            publish: async () => {},
        },
        deliver: async (response) => void delivered.push(response),
        recall: async (request): Promise<LarkRecallOutcome> => {
            recalled.push(request);
            return { kind: 'settled', status: 'recalled', recalled: 1, failed: 0 };
        },
        observeQueueDelay: () => {},
    };

    return { queues: larkOutboundQueues(deps), delivered, recalled, acked, nacked };
}

function idOf(msg: ConsumeMessage): string {
    return String((msg as unknown as { id?: string }).id ?? '?');
}

function message(id: string, body: unknown): ConsumeMessage {
    return {
        id,
        content: Buffer.from(JSON.stringify(body)),
        fields: { consumerTag: 'tag-1' },
        properties: { headers: {} },
    } as unknown as ConsumeMessage;
}

function bindingOn(assembled: Assembled, queue: string) {
    const found = assembled.queues.find((b) => b.route.queue === queue);
    if (!found) throw new Error(`nothing subscribes ${queue}; assembled: ${queueNames(assembled)}`);
    return found;
}

function queueNames(assembled: Assembled): string {
    return assembled.queues.map((b) => b.route.queue).join(', ');
}

describe('lark-outbound 订的就是这两条队列', () => {
    it('回复和撤回都在装配里，一条都不少', () => {
        // 少一条就是整条链被静默摘掉。顺序也钉住：日志和 metrics 的阶段标签按它排。
        const assembled = assemble();

        expect(assembled.queues.map((b) => b.route.queue)).toEqual([
            contractQueue('chat_response'),
            contractQueue('recall'),
        ]);
    });

    it('每条队列接的是自己那条链，没有接串', () => {
        // 光比队列名不够：两条 binding 的 route 写对、handler 接错，表现是回复走了
        // 撤回的处理逻辑 —— 名字层面完全看不出来。
        const assembled = assemble();

        const response: LarkChatResponse = {
            channel: 'lark',
            session_id: 'sess-1',
            message_id: 'cm_trigger',
            chat_id: 'cc_group',
            is_p2p: false,
            content: '在的',
            status: 'success',
            part_index: 0,
            is_last: true,
            bot_name: 'chiwei',
        };
        const payload: LarkRecallPayload = {
            channel: 'lark',
            session_id: 'sess-1',
            // 两种定位方式恰好用一种，这一条走会话那边。
            outbound_id: null,
            reason: 'unsafe',
        };

        const chatResponse = bindingOn(assembled, contractQueue('chat_response'));
        const recall = bindingOn(assembled, contractQueue('recall'));

        return (async () => {
            await chatResponse.handler(chatResponse.route.queue)(message('m1', response));
            await recall.handler(recall.route.queue)(message('m2', payload));

            expect(assembled.delivered).toEqual([response]);
            expect(assembled.recalled.map((r) => r.payload)).toEqual([payload]);
            expect(assembled.acked).toEqual(['m1', 'm2']);
            expect(assembled.nacked).toEqual([]);
        })();
    });
});
