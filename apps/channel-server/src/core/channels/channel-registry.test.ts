import { describe, it, expect } from 'bun:test';
import {
    resolveChannelTriple,
    isKnownChannel,
    resolveBotChannelTriples,
} from './channel-registry';
import type { InboundAdapter, OutboundAdapter } from './contracts';
import type { AddressingPolicy } from './contracts';

// channel-registry 是 bot 加载链路按 bot_config.channel 分发到对应三件套
// (InboundAdapter / OutboundAdapter / AddressingPolicy) 的唯一入口。
// 它 channel 无关：飞书是已落地 adapter，qq 是 T6 占位（必须能被识别、不挂）。
describe('channel-registry: bot 加载链路按 channel 分发', () => {
    it('channel="lark" 能解析出飞书三件套且方法齐全', () => {
        const triple = resolveChannelTriple('lark');
        expect(triple).not.toBeNull();
        const inbound: InboundAdapter = triple!.inbound;
        const outbound: OutboundAdapter = triple!.outbound;
        const policy: AddressingPolicy = triple!.addressing;
        expect(typeof inbound.parse).toBe('function');
        expect(typeof inbound.verify).toBe('function');
        expect(typeof inbound.handleHandshake).toBe('function');
        expect(typeof outbound.send).toBe('function');
        expect(typeof outbound.reply).toBe('function');
        expect(typeof policy.decide).toBe('function');
    });

    it('channel="qq" 是已知 channel：被识别、解析不抛错（T6 占位三件套）', () => {
        expect(isKnownChannel('qq')).toBe(true);
        expect(() => resolveChannelTriple('qq')).not.toThrow();
        const triple = resolveChannelTriple('qq');
        expect(triple).not.toBeNull();
        // 占位 adapter 方法存在但调用即明确抛 "not implemented"，绝不静默
        expect(typeof triple!.inbound.parse).toBe('function');
        expect(() => triple!.inbound.parse({})).toThrow(/not implemented|未实现/i);
    });

    it('未知 channel fail-closed：不返回三件套，明确报错而不是静默', () => {
        expect(isKnownChannel('telegram')).toBe(false);
        expect(() => resolveChannelTriple('telegram')).toThrow(
            /unknown channel|未知 channel/i,
        );
    });
});

// 加载链路真实接入：multiBotManager.initialize() 加载完 bot 列表后必须
// 对每条记录按 channel 解析三件套并校验，未知 channel fail-closed。
describe('resolveBotChannelTriples: bot 加载链路真实分发', () => {
    it('飞书 bot 正常解析到 lark 三件套（按 bot_name 索引）', () => {
        const map = resolveBotChannelTriples([
            { bot_name: 'chiwei', channel: 'lark' },
        ]);
        expect(map.has('chiwei')).toBe(true);
        expect(typeof map.get('chiwei')!.inbound.parse).toBe('function');
    });

    it('channel=qq 的记录能被加载链路识别（解析到 qq 占位三件套、不挂）', () => {
        const map = resolveBotChannelTriples([
            { bot_name: 'chiwei', channel: 'lark' },
            { bot_name: 'qqbot', channel: 'qq' },
        ]);
        expect(map.has('qqbot')).toBe(true);
        // 占位三件套被装配，但任何方法调用即抛错（绝不静默）
        expect(() => map.get('qqbot')!.inbound.parse({})).toThrow(
            /not implemented/i,
        );
    });

    it('未知 channel 在加载期 fail-closed 抛错（不让 bot 半死不活地起来）', () => {
        expect(() =>
            resolveBotChannelTriples([
                { bot_name: 'chiwei', channel: 'lark' },
                { bot_name: 'weird', channel: 'telegram' },
            ]),
        ).toThrow(/unknown channel.*telegram|telegram/i);
    });
});
