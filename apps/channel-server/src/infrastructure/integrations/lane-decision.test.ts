// 入站分流决策逻辑单测。
// 零回归红线：flag off 必须完全旁路 —— 不调 resolveLane、不算 lane、直接 local。

import { describe, it, expect } from 'bun:test';
import { resolveInboundDispatch } from './lane-decision';

describe('resolveInboundDispatch（入站分流决策）', () => {
    it('flag off → 完全旁路：不调 resolveLane，action=local', async () => {
        let called = false;
        const r = await resolveInboundDispatch({
            flagEnabled: false,
            handedOff: false,
            currentLane: 'prod',
            channel: 'qq',
            botGlobalId: 'bot-1',
            commonConversationId: '018f-chat',
            resolveLane: async () => {
                called = true;
                return 'ppe-foo';
            },
        });
        expect(r.action).toBe('local');
        expect(called).toBe(false);
    });

    it('flag on + 决策出 prod → 本地处理（不发 MQ）', async () => {
        const r = await resolveInboundDispatch({
            flagEnabled: true,
            handedOff: false,
            currentLane: 'prod',
            channel: 'qq',
            botGlobalId: 'bot-1',
            commonConversationId: '018f-chat',
            resolveLane: async () => 'prod',
        });
        expect(r.action).toBe('local');
        expect(r.lane).toBe('prod');
    });

    it('flag on + 决策出非本进程 lane → 投 MQ', async () => {
        const r = await resolveInboundDispatch({
            flagEnabled: true,
            handedOff: false,
            currentLane: 'prod',
            channel: 'qq',
            botGlobalId: 'bot-1',
            commonConversationId: '018f-chat',
            resolveLane: async () => 'ppe-foo',
        });
        expect(r.action).toBe('dispatch');
        expect(r.lane).toBe('ppe-foo');
    });

    it('flag on + 决策出的 lane 恰是本进程 lane（非 prod 进程）→ 本地，不自投自', async () => {
        const r = await resolveInboundDispatch({
            flagEnabled: true,
            handedOff: false,
            currentLane: 'ppe-foo',
            channel: 'qq',
            botGlobalId: 'bot-1',
            commonConversationId: '018f-chat',
            resolveLane: async () => 'ppe-foo',
        });
        expect(r.action).toBe('local');
    });

    it('flag on + 非 prod 进程收到交接来的信封 → 信封 lane 已定，不再二次 resolve/dispatch', async () => {
        let called = false;
        const r = await resolveInboundDispatch({
            flagEnabled: true,
            handedOff: false,
            currentLane: 'ppe-foo',
            channel: 'qq',
            botGlobalId: 'bot-1',
            commonConversationId: '018f-chat',
            resolveLane: async () => {
                called = true;
                return 'prod';
            },
        });

        expect(r).toEqual({ action: 'local', lane: 'ppe-foo' });
        expect(called).toBe(false);
    });

    it('已交接的信封 → local，绝不再算一次 lane（自投循环的阻断点）', async () => {
        let called = false;
        const r = await resolveInboundDispatch({
            flagEnabled: true,
            handedOff: true,
            currentLane: 'prod',
            channel: 'qq',
            botGlobalId: 'bot-1',
            commonConversationId: '018f-chat',
            resolveLane: async () => {
                called = true;
                return 'ppe-foo';
            },
        });

        expect(r.action).toBe('local');
        expect(called).toBe(false);
    });

    it('flag on → 把 commonConversationId 传给 resolveLane', async () => {
        let seenConversationId: string | undefined;
        await resolveInboundDispatch({
            flagEnabled: true,
            handedOff: false,
            currentLane: 'prod',
            channel: 'qq',
            botGlobalId: 'bot-1',
            commonConversationId: '018f-chat',
            resolveLane: async (_channel, _bot, commonConversationId) => {
                seenConversationId = commonConversationId;
                return 'prod';
            },
        });
        expect(seenConversationId).toBe('018f-chat');
    });
});
