// 主动发 message_id 前缀的跨语言线格式契约（消费方这一侧）。
//
// `proactive:<uuid>` 这个形状由 agent-service（Python）产出、由本服务的出站投递
// 剥前缀取 uuid 落进 common_message.agent_outbound_id。前缀是两边共同的约定，
// 但两边各写各的字面量 —— 只改一边不会有任何测试变红：投递方静默认不出主动消息，
// 那次开口在库里永久失联，全程零报错。
//
// 所以两侧测试读同一份向量：contracts/proactive-message-id.json。要骗过测试就得改
// 共享的那一份，而改了共享那一份，两侧一起转红。
//
// 读它的是测试、不是生产代码：两个镜像的 Dockerfile 都不 COPY contracts/，
// 本服务还是 `bun build --compile` 出来的独立二进制（import.meta.dir 在运行时是
// /$bunfs/root），运行时根本读不到这份文件。跟 contracts/mq-channel-routes.json
// 是同一套做法。
//
// Python 侧：apps/agent-service/tests/domain/test_proactive_message_id_contract.py

import { describe, expect, it } from 'bun:test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

import { PROACTIVE_MESSAGE_ID_PREFIX } from './deliver';

interface ProactiveMessageIdContract {
    message_id_prefix: string;
    outbound_id_vector: {
        hex: string;
        uuid: string;
        message_id: string;
    };
}

const CONTRACT_PATH = resolve(
    import.meta.dir,
    '../../../../../contracts/proactive-message-id.json',
);
const contract = JSON.parse(
    readFileSync(CONTRACT_PATH, 'utf8'),
) as ProactiveMessageIdContract;

describe('主动发 message_id 前缀 — 跨语言契约', () => {
    it('本服务的前缀常量与契约逐字一致', () => {
        expect(PROACTIVE_MESSAGE_ID_PREFIX).toBe(contract.message_id_prefix);
    });
});

describe('一次开口的两种写法 — 跨语言契约', () => {
    // agent-service 那侧一次派生、两处取值：32 位无短横 hex 记进她自己的台账、也是
    // 她在快照和会话里**唯一见得到**的写法；带短横的标准 uuid 走线格式，本服务剥掉
    // 前缀之后落进 common_message.agent_outbound_id（列是 uuid 类型，hex 进不去）。
    //
    // 两种写法是同一个值这件事，此前两侧都没有测试证明 —— 契约里只有前缀。换算错了
    // 全程零报错：她照抄的编号查不到任何行，撤回只说"没有这条"。

    it('线上传的那一串 = 前缀 + 带短横的写法', () => {
        const v = contract.outbound_id_vector;
        expect(v.message_id).toBe(`${PROACTIVE_MESSAGE_ID_PREFIX}${v.uuid}`);
        // deliver.ts 的 agentOutboundIdOf 就是剥掉前缀取这一段落进列里。
        expect(v.message_id.slice(PROACTIVE_MESSAGE_ID_PREFIX.length)).toBe(v.uuid);
    });

    it('落进列里的那一种去掉短横就是她见到的那一种', () => {
        const v = contract.outbound_id_vector;
        expect(v.uuid.replace(/-/g, '')).toBe(v.hex);
        expect(v.hex).toMatch(/^[0-9a-f]{32}$/);
        expect(v.uuid).toBe(v.uuid.toLowerCase());
    });
});
