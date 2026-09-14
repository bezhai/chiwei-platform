import { describe, expect, it } from 'bun:test';
import { receiveLarkMemberChange, type LarkMemberChangeDeps } from './directory-events';
import type { LarkEvent } from './ingress/lark-event';

const event: LarkEvent = {
    type: 'im.chat.member.user.added_v1',
    botName: 'dev',
    receivedAt: new Date(),
    payload: { chat_id: 'oc_1', event_id: 'ev_1', users: [{ user_id: { union_id: 'on_1' } }] },
};
function harness(over: Partial<LarkMemberChangeDeps> = {}) {
    const syncs: unknown[] = [],
        envelopes: unknown[] = [];
    const deps: LarkMemberChangeDeps = {
        currentLane: 'prod',
        laneDispatchEnabled: async () => true,
        conversationOf: async () => 'cc_1',
        laneOf: async () => 'prod',
        handOff: async (envelope) => {
            envelopes.push(envelope);
        },
        sync: async (chatId, humanIds) => {
            syncs.push([chatId, humanIds]);
        },
        newId: () => 'generated-id',
        ...over,
    };
    return { deps, syncs, envelopes };
}
describe('成员事件的泳道归属', () => {
    it('在生产仅只读查询归属，交接到测试泳道后才同步', async () => {
        const h = harness({
            laneOf: async (bot, conversation) => {
                expect([bot, conversation]).toEqual(['dev', 'cc_1']);
                return 'coe-members';
            },
        });
        await receiveLarkMemberChange(h.deps, event);
        expect(h.syncs).toEqual([]);
        expect(h.envelopes).toEqual([
            expect.objectContaining({
                event_type: event.type,
                global_message_id: 'ev_1',
                lane: 'coe-members',
                params: event.payload,
                handed_off: true,
            }),
        ]);
    });
    it('交接落回生产也只同步一次，不再分发', async () => {
        const h = harness({
            laneOf: async () => {
                throw new Error('must not resolve');
            },
        });
        await receiveLarkMemberChange(h.deps, { ...event, handedOff: true });
        expect(h.syncs).toEqual([['oc_1', ['on_1']]]);
        expect(h.envelopes).toEqual([]);
    });
    it('机器人入群不把机器人ID作为真人证据', async () => {
        const h = harness();
        await receiveLarkMemberChange(h.deps, { ...event, type: 'im.chat.member.bot.added_v1' });
        expect(h.syncs).toEqual([['oc_1', []]]);
    });
    it('没有已建群映射时仍按bot绑定交接', async () => {
        const h = harness({
            conversationOf: async () => undefined,
            laneOf: async (_bot, cc) => {
                expect(cc).toBeUndefined();
                return 'coe-members';
            },
        });
        await receiveLarkMemberChange(h.deps, event);
        expect(h.envelopes).toHaveLength(1);
        expect(h.syncs).toHaveLength(0);
    });
    it('同步失败抛给入口处理', async () => {
        const h = harness({
            sync: async () => {
                throw new Error('API denied');
            },
        });
        await expect(receiveLarkMemberChange(h.deps, event)).rejects.toThrow('API denied');
    });
    it('缺少chat_id拒绝且不访问API', async () => {
        const h = harness();
        await expect(receiveLarkMemberChange(h.deps, { ...event, payload: {} })).rejects.toThrow(
            'chat_id',
        );
        expect(h.syncs).toEqual([]);
    });
});
