// 卡片按钮被按下之后。
//
// 这是**第二条入站路径**，跟指令那条完全不搭界：它不过规则引擎、不看 @、不看权限
// 白名单，只认按钮里带回来的那份 value。三种动作各自打不同的开放平台端点。
//
// 拆分之后这条路曾经整条断着：路由注册了、事件槽也标成了 card.action.trigger，但入站
// 的事件处理表里只有消息接收 —— 回调进来打一条"没人处理"的 warn 就没了。所以本文件
// 除了三种动作各一条，还要有一条"不认识的动作不会炸"。

import { describe, expect, it } from 'bun:test';
import type { ImageForLark } from '@inner/pixiv-client';

import type { LarkChatRow } from '../projection/tables';
import { handleLarkCardAction, type LarkCardCallbackDeps } from './callback';
import {
    FETCH_PHOTO_DETAILS,
    UPDATE_DAILY_PHOTO_CARD,
    UPDATE_PHOTO_CARD,
    type LarkCardActionValue,
} from './card-actions';
import type { LarkReadyPhotos } from './ready';

const PHOTOS: ImageForLark[] = [
    { pixiv_addr: 'a.png', image_key: 'a', width: 100, height: 100, author: '甲' },
    { pixiv_addr: 'b.png', image_key: 'b', width: 100, height: 100, author: '乙' },
];

function action(value: LarkCardActionValue | undefined) {
    return {
        action: { tag: 'button', value },
        context: { open_message_id: 'om_card', open_chat_id: 'oc_1' },
        operator: { open_id: 'ou_presser', union_id: 'on_presser', user_id: 'u1' },
        token: 'card-token',
    };
}

function rig(
    options: { chat?: LarkChatRow | null; photos?: LarkReadyPhotos } = {},
) {
    const requested: { method: string; path: string; body: any }[] = [];
    const replied: { messageId: string; card: object; inThread: boolean }[] = [];
    const asked: unknown[] = [];

    const deps = {
        api: {
            replyCard: async (messageId: string, card: object, inThread: boolean) => {
                replied.push({ messageId, card, inThread });
                return {};
            },
            request: async (method: string, path: string, body: unknown) => {
                requested.push({ method, path, body });
                return {};
            },
        },
        store: {
            larkChat: async () => options.chat ?? null,
        },
        photos:
            options.photos ??
            (async (query) => {
                asked.push(query);
                return PHOTOS;
            }),
    } as unknown as LarkCardCallbackDeps;

    return { deps, requested, replied, asked };
}

const IN_A_GROUP: LarkChatRow = { chat_id: 'oc_1', chat_mode: 'group' };
const IN_A_DM: LarkChatRow = { chat_id: 'oc_1', chat_mode: 'p2p' };

// ---------------------------------------------------------------------------

describe('「换一批」', () => {
    it('用原来的标签重搜，走延时更新接口', async () => {
        const it_ = rig({ chat: IN_A_GROUP });

        await handleLarkCardAction(
            it_.deps,
            action({ type: UPDATE_PHOTO_CARD, tags: ['刻晴'] }),
        );

        expect(it_.asked).toEqual([expect.objectContaining({ tag_and_author: ['刻晴'] })]);
        expect(it_.requested).toHaveLength(1);
        expect(it_.requested[0]!.method).toBe('POST');
        expect(it_.requested[0]!.path).toBe('/open-apis/interactive/v1/card/update');
    });

    // token 是这次交互一次性的凭证，open_ids 决定只有按按钮的人看到新卡片 ——
    // 少了它整个群会看到别人换出来的那一批。
    it('带上一次性 token，只更新给按按钮的那个人', async () => {
        const it_ = rig({ chat: IN_A_GROUP });

        await handleLarkCardAction(
            it_.deps,
            action({ type: UPDATE_PHOTO_CARD, tags: ['刻晴'] }),
        );

        const body = it_.requested[0]!.body;
        expect(body.token).toBe('card-token');
        expect(body.card.open_ids).toEqual(['ou_presser']);
        // 延时更新只吃 V1 的 elements 数组，不是整张卡片。
        expect(Array.isArray(body.card.elements)).toBe(true);
        expect(body.card.elements[0].tag).toBe('column_set');
    });

    it('这个会话的 allow_send_limit_photo 说了算', async () => {
        const it_ = rig({
            chat: { ...IN_A_GROUP, permission_config: { allow_send_limit_photo: true } },
        });

        await handleLarkCardAction(
            it_.deps,
            action({ type: UPDATE_PHOTO_CARD, tags: ['刻晴'] }),
        );

        expect(it_.asked).toEqual([expect.objectContaining({ status: 0 })]);
    });

    // 一张都没搜到时图库会抛。这条路没有"对着用户说一句"的地方（回调不是消息），
    // 所以只能吞掉记日志 —— 与拆分前一致。
    it('搜不到图时什么都不发，也不外溢', async () => {
        const it_ = rig({ chat: IN_A_GROUP, photos: async () => [] });

        await handleLarkCardAction(
            it_.deps,
            action({ type: UPDATE_PHOTO_CARD, tags: ['没有的标签'] }),
        );

        expect(it_.requested).toEqual([]);
    });
});

describe('「今日新图」的「换一批」', () => {
    it('按时间下界重搜，同样走延时更新', async () => {
        const it_ = rig({ chat: IN_A_GROUP });

        await handleLarkCardAction(
            it_.deps,
            action({ type: UPDATE_DAILY_PHOTO_CARD, start_time: 1_700_000_000_000 }),
        );

        expect(it_.asked).toEqual([
            expect.objectContaining({ start_time: 1_700_000_000_000 }),
        ]);
        expect(it_.requested[0]!.path).toBe('/open-apis/interactive/v1/card/update');
        // 换出来的还得是那张带绿色标题的「今日新图」。
        expect(it_.requested[0]!.body.card.elements).toBeDefined();
    });
});

describe('「查看详情」', () => {
    // 群里详情只给按按钮的人看：直接回复的话，一个人好奇会刷屏整个群。
    it('群聊走"仅操作者可见"，带上他的 open_id', async () => {
        const it_ = rig({ chat: IN_A_GROUP });

        await handleLarkCardAction(
            it_.deps,
            action({ type: FETCH_PHOTO_DETAILS, images: ['a.png', 'b.png'] }),
        );

        expect(it_.replied).toEqual([]);
        expect(it_.requested).toHaveLength(1);
        expect(it_.requested[0]!.path).toBe('/open-apis/ephemeral/v1/send');
        expect(it_.requested[0]!.body.chat_id).toBe('oc_1');
        expect(it_.requested[0]!.body.msg_type).toBe('interactive');
        expect(it_.requested[0]!.body.open_id).toBe('ou_presser');
        expect(it_.requested[0]!.body.card).toBeDefined();
    });

    it('私聊直接挂在卡片那条消息上回复', async () => {
        const it_ = rig({ chat: IN_A_DM });

        await handleLarkCardAction(
            it_.deps,
            action({ type: FETCH_PHOTO_DETAILS, images: ['a.png'] }),
        );

        expect(it_.requested).toEqual([]);
        expect(it_.replied).toHaveLength(1);
        expect(it_.replied[0]!.messageId).toBe('om_card');
        expect(it_.replied[0]!.inThread).toBe(false);
    });

    // 会话行还没建过（第一条消息之前就点了按钮）时按私聊处理，与拆分前一致。
    it('查不到会话行时也直接回复', async () => {
        const it_ = rig({ chat: null });

        await handleLarkCardAction(
            it_.deps,
            action({ type: FETCH_PHOTO_DETAILS, images: ['a.png'] }),
        );

        expect(it_.replied).toHaveLength(1);
    });

    it('按卡片上那批地址查，包括已经删掉的', async () => {
        const it_ = rig({ chat: IN_A_DM });

        await handleLarkCardAction(
            it_.deps,
            action({ type: FETCH_PHOTO_DETAILS, images: ['a.png', 'b.png'] }),
        );

        expect(it_.asked).toEqual([
            expect.objectContaining({ pixiv_addrs: ['a.png', 'b.png'], status: 3 }),
        ]);
    });

    it('一张都查不到时什么都不发，也不外溢', async () => {
        const it_ = rig({ chat: IN_A_DM, photos: async () => [] });

        await handleLarkCardAction(
            it_.deps,
            action({ type: FETCH_PHOTO_DETAILS, images: ['gone.png'] }),
        );

        expect(it_.replied).toEqual([]);
        expect(it_.requested).toEqual([]);
    });
});

describe('认不出来的回调', () => {
    // 别的卡片（别人发的、老版本的）也会打到同一个回调上。炸掉的话整个入口会开始
    // 记 error，而且什么都没坏。
    it.each([
        ['没有 value', undefined],
        ['type 不认识', { type: 'something-else' } as unknown as LarkCardActionValue],
    ])('%s：什么都不做，不抛', async (_name, value) => {
        const it_ = rig({ chat: IN_A_GROUP });

        await handleLarkCardAction(it_.deps, action(value));

        expect(it_.requested).toEqual([]);
        expect(it_.replied).toEqual([]);
    });

    // 报文根本不是一次卡片交互（字段缺光）时同样不该炸。
    it('报文缺字段也不抛', async () => {
        const it_ = rig();

        await handleLarkCardAction(it_.deps, { not: 'a card action' });

        expect(it_.requested).toEqual([]);
    });
});

describe('打飞书失败', () => {
    it('延时更新被拒时吞掉（回调没有能对着说话的人）', async () => {
        const it_ = rig({ chat: IN_A_GROUP });
        (it_.deps.api as any).request = async () => {
            throw new Error('card token expired');
        };

        await handleLarkCardAction(
            it_.deps,
            action({ type: UPDATE_PHOTO_CARD, tags: ['刻晴'] }),
        );
    });
});
