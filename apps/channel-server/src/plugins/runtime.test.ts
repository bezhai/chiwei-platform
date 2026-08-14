// 泳道信封按信封自己的 channel 找 runtime。
//
// 这里只钉一件事：**不许猜**。信封曾经在缺 channel 时按飞书算，那在飞书还归本服务的
// 时候至少能跑通；现在本服务只注册了 QQ 的 runtime，猜飞书的下场是 getChannelRuntime
// 抛一句 "unknown channel runtime lark" —— 报错内容指向注册表，而真正的问题在信封。

// runtime 注册表是模块级的，注册进去就撤不掉（application.test.ts 拿它算 handles）。
// 所以这里只跑不需要注册任何 runtime 的那条路：信封说不出自己是哪个 channel 时的处置。
// 「按 channel 派给对应 runtime」由各插件自己的用例和 inbound-lane-consumer 的接线
// 测试覆盖。

import { describe, expect, it } from 'bun:test';

import type { InboundLaneEnvelope } from '@integrations/inbound-lane';
import { handleInboundLaneEnvelope } from './runtime';

const envelope: InboundLaneEnvelope = {
    channel: 'qq',
    event_type: 'im.message.receive_v1',
    global_message_id: 'gmid-1',
    trace_id: 'trace-1',
    lane: 'ppe-foo',
    bot_name: 'chiwei',
    params: {},
};

describe('handleInboundLaneEnvelope', () => {
    it('信封没有 channel：抛错，不回落到某个渠道', async () => {
        const { channel: _channel, ...noChannel } = envelope;

        await expect(
            handleInboundLaneEnvelope(noChannel as InboundLaneEnvelope),
        ).rejects.toThrow(/carries no channel/);
    });
});
