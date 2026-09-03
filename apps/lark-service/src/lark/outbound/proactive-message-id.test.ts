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
