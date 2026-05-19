import { describe, it, expect } from 'bun:test';

import { runInboundContractChain } from './inbound-pipeline';
import { InMemoryIdentityResolver } from './identity-resolver';
import { LarkInboundAdapter, LarkAddressingPolicy } from './lark/lark-adapter';
import type { LarkReceiveMessage } from 'types/lark';

// 必改1 钉死：codex 质疑「非 @bot 群消息照常入库+复读」零变化硬约束
// 没真正闭环 —— 怀疑真实契约链对非 @bot 群消息会短路（chain.ok=false /
// skip），而 handlers.inbound-order.test.ts 把 chain 固定 mock 成
// ok:true/respond:true，掩盖了真实语义。
//
// 本测试用**真实** LarkInboundAdapter.parse + **真实** LarkAddressingPolicy
// .decide + **真实** enforceDecision，过**真实** runInboundContractChain，
// 喂一条**群里没有 @bot 的真实飞书文本事件**，钉死真实语义：
//
//   LarkAddressingPolicy 对非 @bot 群消息返回 respond:false 且 reason
//   **非空**（'group message without bot ... mention; not addressed to
//   bot'）→ enforceDecision 不抛错（空 reason 才抛）→ runInboundContract
//   Chain 不进 catch 分支 → 返回 ok:true, respond:false，且 resolve 仍
//   翻全局 ID。即真实链路对非 @bot 群消息**不短路**：handlers.ts 只看
//   chain.ok（=true）就照常 runRules → storeMessage，复读 + 入库不被丢。
//
// 这消除 codex 指出的 mock 盲区：非 @bot 群消息的 ok/respond 由真实
// LarkAddressingPolicy 决定，而非黑盒固定 ok:true/respond:true。

const adapter = new LarkInboundAdapter();
const policy = new LarkAddressingPolicy();
const BOT_UNION_ID = 'on_bot_union_xyz';

// 一条群里没有 @bot 的真实飞书文本事件（mentions 为空）。
function nonMentionGroupEvent(): LarkReceiveMessage {
    return {
        app_id: 'cli_x',
        sender: { sender_id: { union_id: 'on_user_aaa' } },
        message: {
            message_id: 'om_real_1',
            chat_id: 'oc_real_1',
            chat_type: 'group',
            message_type: 'text',
            create_time: '1700000000000',
            content: JSON.stringify({ text: '大家早上好' }),
            mentions: [],
        },
    } as unknown as LarkReceiveMessage;
}

describe('必改1: real LarkAddressingPolicy non-@bot group message does NOT short-circuit', () => {
    it('ok:true respond:false, global ids resolved, skip reason logged (no contract_chain_error)', async () => {
        let skipReason = '';
        const res = await runInboundContractChain({
            params: nonMentionGroupEvent(),
            parse: (raw) => adapter.parse(raw as LarkReceiveMessage),
            decide: (m, b) => policy.decide(m, b),
            botIdentity: BOT_UNION_ID,
            resolver: new InMemoryIdentityResolver(),
            logSkip: (r) => {
                skipReason = r;
            },
        });

        // 真实链路结论：ok=true（不是 parsed_null，也不是
        // contract_chain_error 短路），respond=false。
        expect(res.ok).toBe(true);
        if (res.ok) {
            expect(res.respond).toBe(false);
            // resolve 仍翻全局 ID —— 飞书 native 链路（复读 / storeMessage）
            // 对非 @bot 群消息照常运行的数据来源。
            expect(res.globalMessageId).toBeTruthy();
            expect(res.globalChatId).toBeTruthy();
            expect(res.globalUserId).toBeTruthy();
        }
        // enforceDecision 收到非空 reason → 记可查日志、不抛错（空 reason
        // 才抛 → 才会进 catch → 才 ok:false 短路）。
        expect(skipReason.length).toBeGreaterThan(0);
        expect(skipReason).toContain('not addressed to bot');
    });

    it('@bot group message via real policy: ok:true respond:true', async () => {
        // 对照组：群里 @ 了 bot（mentions 含 bot union_id）→ respond:true。
        const ev = nonMentionGroupEvent();
        (ev.message as { mentions: unknown[] }).mentions = [
            { id: { union_id: BOT_UNION_ID } },
        ];
        const res = await runInboundContractChain({
            params: ev,
            parse: (raw) => adapter.parse(raw as LarkReceiveMessage),
            decide: (m, b) => policy.decide(m, b),
            botIdentity: BOT_UNION_ID,
            resolver: new InMemoryIdentityResolver(),
            logSkip: () => undefined,
        });
        expect(res.ok).toBe(true);
        if (res.ok) expect(res.respond).toBe(true);
    });
});
