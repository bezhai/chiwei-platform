// 消费侧从 AMQP header 还原跨进程上下文的口径单测。
//
// 这是 rabbitmq.ts::publish 注入侧（rabbitmq.test.ts 覆盖）的对侧：写入什么格式，
// 这里就必须按同样的格式读回来。归一规则必须与 agent-service
// app/runtime/propagation.py 的 _coerce 完全一致，否则同一条消息在 TS worker 和
// Python 服务里会解出两个不同的 lane。

import { describe, it, expect } from 'bun:test';
import type { ConsumeMessage } from 'amqplib';

import { laneFromMessage, traceIdFromMessage } from './context';

function msgWithHeaders(headers: Record<string, unknown> | undefined): ConsumeMessage {
    return {
        content: Buffer.from('{}'),
        fields: {} as ConsumeMessage['fields'],
        properties: { headers } as unknown as ConsumeMessage['properties'],
    } as ConsumeMessage;
}

describe('laneFromMessage', () => {
    it('header 带非空字符串 lane：原样返回', () => {
        expect(laneFromMessage(msgWithHeaders({ lane: 'ppe-taskb' }))).toBe('ppe-taskb');
        expect(laneFromMessage(msgWithHeaders({ lane: 'coe-x' }))).toBe('coe-x');
        // 'prod' 不在这里被特殊处理：publish 侧对 prod 写的是空串，真出现字面
        // 'prod' 说明上游另有约定，如实透出而不是偷偷归一。
        expect(laneFromMessage(msgWithHeaders({ lane: 'prod' }))).toBe('prod');
    });

    it('header lane 是空串：无 lane（inject_context 对「无 lane」写的就是空串）', () => {
        expect(laneFromMessage(msgWithHeaders({ lane: '' }))).toBeUndefined();
    });

    it('headers 里没有 lane key：无 lane', () => {
        expect(laneFromMessage(msgWithHeaders({ trace_id: 't-1' }))).toBeUndefined();
    });

    it('整个 headers 缺失：无 lane（部署窗口内的在途旧消息走这条）', () => {
        expect(laneFromMessage(msgWithHeaders(undefined))).toBeUndefined();
    });

    it('lane 不是字符串：无 lane（与 Python _coerce 对齐，不做 String() 强转）', () => {
        expect(laneFromMessage(msgWithHeaders({ lane: 42 }))).toBeUndefined();
        expect(laneFromMessage(msgWithHeaders({ lane: null }))).toBeUndefined();
        expect(laneFromMessage(msgWithHeaders({ lane: true }))).toBeUndefined();
        expect(laneFromMessage(msgWithHeaders({ lane: ['ppe-taskb'] }))).toBeUndefined();
        expect(laneFromMessage(msgWithHeaders({ lane: { v: 'ppe-taskb' } }))).toBeUndefined();
        // amqplib 会把 longstr header 解成 Buffer；Buffer 不是 string，同样按无 lane 处理。
        expect(laneFromMessage(msgWithHeaders({ lane: Buffer.from('ppe-taskb') }))).toBeUndefined();
    });
});

describe('traceIdFromMessage', () => {
    it('header 带非空字符串 trace_id：原样返回', () => {
        expect(traceIdFromMessage(msgWithHeaders({ trace_id: 't-1' }))).toBe('t-1');
    });

    it('空串 / 缺 key / 无 headers / 非字符串：都当没有 trace（与 lane 同一套归一规则）', () => {
        expect(traceIdFromMessage(msgWithHeaders({ trace_id: '' }))).toBeUndefined();
        expect(traceIdFromMessage(msgWithHeaders({ lane: 'ppe-taskb' }))).toBeUndefined();
        expect(traceIdFromMessage(msgWithHeaders(undefined))).toBeUndefined();
        expect(traceIdFromMessage(msgWithHeaders({ trace_id: 42 }))).toBeUndefined();
        expect(
            traceIdFromMessage(msgWithHeaders({ trace_id: Buffer.from('t-1') })),
        ).toBeUndefined();
    });

    it('lane 和 trace_id 各读各的 key，互不串味', () => {
        const msg = msgWithHeaders({ lane: 'ppe-taskb', trace_id: 't-1' });
        expect(laneFromMessage(msg)).toBe('ppe-taskb');
        expect(traceIdFromMessage(msg)).toBe('t-1');
    });
});
