import { describe, expect, it } from 'bun:test';
import { receiveLarkMemberChange, type LarkMemberChangeDeps } from './directory-events';
import type { LarkEvent } from './ingress/lark-event';

const event: LarkEvent = {
    type: 'im.chat.member.user.added_v1',
    botName: 'dev',
    receivedAt: new Date(),
    payload: { chat_id: 'oc_1', event_id: 'ev_1', create_time: '1789385020220', users: [{ name: '张若', user_id: { union_id: 'on_1' } }] },
};
function harness(over: Partial<LarkMemberChangeDeps> = {}) {
    const changes: unknown[] = [],
        envelopes: unknown[] = [];
    const deps: LarkMemberChangeDeps = {
        currentLane: 'prod',
        laneDispatchEnabled: async () => true,
        conversationOf: async () => 'cc_1',
        laneOf: async () => 'prod',
        handOff: async (envelope) => {
            envelopes.push(envelope);
        },
        changeMembers: async (...args) => { changes.push(args); },
        newId: () => 'generated-id',
        ...over,
    };
    return { deps, changes, envelopes };
}
describe('成员事件的泳道归属', () => {
    it('退群和撤销加群只更新事件中的用户并传递真实事件时间', async () => {
        for (const type of ['im.chat.member.user.deleted_v1', 'im.chat.member.user.withdrawn_v1']) {
            const h = harness();
            await receiveLarkMemberChange(h.deps, { ...event, type });
            expect(h.changes).toEqual([['oc_1', [{unionId: 'on_1', name: '张若'}], true, new Date(1789385020220)]]);
        }
    });
    it('真人成员事件缺少真实时间或完整身份时拒绝，不能以接收时间覆盖状态', async () => {
        const h = harness();
        for (const overrides of [{create_time: undefined}, {create_time: 'invalid'}, {create_time: 'Infinity'}, {users: [{}]}, {users: [{user_id: {union_id: 'on_1'}, name: ''}]}]) {
            await expect(receiveLarkMemberChange(h.deps, {...event, payload: {...event.payload as object, ...overrides}})).rejects.toThrow();
        }
        expect(h.changes).toEqual([]);
    });
    it('在生产仅只读查询归属，交接到测试泳道后才同步', async () => {
        const h = harness({
            laneOf: async (bot, conversation) => {
                expect([bot, conversation]).toEqual(['dev', 'cc_1']);
                return 'coe-members';
            },
        });
        await receiveLarkMemberChange(h.deps, event);
        expect(h.changes).toEqual([]);
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
        expect(h.changes).toEqual([['oc_1', [{ unionId: 'on_1', name: '张若' }], false, new Date(1789385020220)]]);
        expect(h.envelopes).toEqual([]);
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
        expect(h.changes).toHaveLength(0);
    });
    it('同步失败抛给入口处理', async () => {
        const h = harness({
            changeMembers: async () => {
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
        expect(h.changes).toEqual([]);
    });
});
