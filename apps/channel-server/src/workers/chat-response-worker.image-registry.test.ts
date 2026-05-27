import { describe, it, expect } from 'bun:test';

import { reverseResolveForLark } from '../core/channels/outbound-pipeline';
import { InMemoryIdentityResolver } from '../core/channels/identity-resolver';
import { imageRegistryLookupId } from './image-registry-key';

// 发图被吞回归钉死（trace aae2dd2cacbc711123da9e41d1525e4f）：
//
// agent-service 在 app/chat/context.py 用 ImageRegistry(req.message_id) 把
// generate_image 产出的图片注册到 Redis，key = image_registry:{全局 internal
// message_id ULID}。chat-response-worker 必须用【同一个全局 id】去查 registry。
//
// 多渠道改造（PR #228）后，worker 先把 payload.message_id 用
// reverseResolveForLark 反查成飞书裸 om_* 再查 registry —— 那个裸键 agent-service
// 从来没写过，registry 必 miss，resolveImageReferences 原样返回带 N.png 的文本，
// markdownToPostContent 再静默跳过未解析的 N.png，最终用户什么图都收不到。
//
// 本测试钉死：image registry 的查询 id 是【全局 payload.message_id】，
// 且它跟 reverseResolveForLark 反查出来的飞书裸 id 是不同的字符串 —— 用反查后的
// 裸 id 查 registry 就是这个 bug 本身。

describe('image registry 查询 id 契约：必须用全局 message_id，不能用反查后的飞书裸 id', () => {
    it('registry 查询 id == 全局 payload.message_id（agent-service 注册用的同一个 key）', () => {
        const globalMsg = '01ARZ3NDEKTSV4RRFFQ69G5FAV';
        expect(imageRegistryLookupId({ message_id: globalMsg })).toBe(globalMsg);
    });

    it('全局 message_id 反查出的飞书裸 id 与全局 id 不同 —— 用裸 id 查 registry 必 miss（复现 bug）', async () => {
        const r = new InMemoryIdentityResolver();
        const globalMsg = await r.resolve('message', 'lark', 'om_real_msg');
        const globalChat = await r.resolve('chat', 'lark', 'oc_real_chat');

        const rr = await reverseResolveForLark({
            resolver: r,
            messageGlobalId: globalMsg,
            chatGlobalId: globalChat,
            rootGlobalId: undefined,
        });

        // 反查回的飞书裸 id 就是 om_real_msg，跟全局 ULID 是两个不同字符串
        expect(rr.channelMessageId).toBe('om_real_msg');
        expect(rr.channelMessageId).not.toBe(globalMsg);

        // registry 查询必须用全局 id，绝不能用 rr.channelMessageId（bug 路径）
        const lookupId = imageRegistryLookupId({ message_id: globalMsg });
        expect(lookupId).toBe(globalMsg);
        expect(lookupId).not.toBe(rr.channelMessageId);
    });
});
