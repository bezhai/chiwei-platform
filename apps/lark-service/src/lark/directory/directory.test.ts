import { describe, expect, it } from 'bun:test';
import {
    createLarkDirectory,
    type LarkDirectoryApi,
    type LarkDirectoryStore,
    type LarkDirectoryTables,
    type LarkDirectoryMember,
    type LarkDirectoryProfile,
} from './directory';
import type { LarkMessageEvent } from '../message/wire';
import { createLarkMentionResolver } from '../outbound/mentions';

function event(
    unionId = 'on_new',
    time = 2000,
    chatType = 'group',
    senderType = 'user',
): LarkMessageEvent {
    return {
        sender: {
            sender_type: senderType,
            sender_id: { union_id: unionId, open_id: 'ou_new' },
        },
        message: {
            message_id: 'om_message',
            create_time: String(time),
            chat_id: 'oc_group',
            chat_type: chatType,
            message_type: 'text',
            content: '{"text":"hi"}',
        },
    };
}

function rig() {
    const profiles = new Map<string, LarkDirectoryProfile>();
    const members = new Map<string, LarkDirectoryMember>();
    const calls: string[] = [];
    let tail: Promise<unknown> = Promise.resolve();
    const tables: LarkDirectoryTables = {
        async profile(id) {
            return profiles.get(id) ?? null;
        },
        async member(chatId, id) {
            return (
                members.get(chatId === 'oc_group' ? id : `${chatId}:${id}`) ??
                null
            );
        },
        async fillProfile(profile) {
            if (profiles.get(profile.unionId)?.name.trim()) return;
            calls.push(`profile:${profile.unionId}`);
            profiles.set(profile.unionId, profile);
        },
        async applyMembership(chatId, id, hasLeft, observedAt) {
            calls.push(`member:${id}`);
            const key = chatId === 'oc_group' ? id : `${chatId}:${id}`;
            const old = members.get(key);
            const previous = old?.updatedAt?.getTime();
            if (
                previous !== undefined &&
                (previous > observedAt.getTime() ||
                    (previous === observedAt.getTime() &&
                        (!hasLeft || old?.hasLeft)))
            )
                return false;
            members.set(key, {
                unionId: id,
                name: profiles.get(id)?.name ?? null,
                hasLeft,
                updatedAt: observedAt,
            });
            return true;
        },
    };
    const store: LarkDirectoryStore = {
        ...tables,
        withChatLock(_chatId, run) {
            const next = tail.then(async () => {
                calls.push('lock');
                const oldProfiles = new Map(profiles);
                const oldMembers = new Map(members);
                try {
                    const result = await run(tables);
                    calls.push('commit');
                    return result;
                } catch (error) {
                    profiles.clear();
                    members.clear();
                    for (const [id, value] of oldProfiles)
                        profiles.set(id, value);
                    for (const [id, value] of oldMembers)
                        members.set(id, value);
                    calls.push('rollback');
                    throw error;
                }
            });
            tail = next.catch(() => {});
            return next;
        },
    };
    const api: LarkDirectoryApi = {
        async user(id) {
            calls.push(`contact:${id}`);
            return { unionId: id, name: '新人' };
        },
    };
    function seed(
        id: string,
        name: string | null,
        hasLeft: boolean,
        time: number | null,
    ) {
        if (name !== null) profiles.set(id, { unionId: id, name });
        members.set(id, {
            unionId: id,
            name,
            hasLeft,
            updatedAt: time === null ? null : new Date(time),
        });
    }
    return {
        profiles,
        members,
        calls,
        store,
        api,
        seed,
        directory: createLarkDirectory({ store, api, botUnionIds: ['on_bot'] }),
    };
}

describe('incremental Lark directory', () => {
    it('uses join event names without contact or roster APIs and keeps unrelated members unchanged', async () => {
        const h = rig();
        for (let i = 0; i < 101; i++)
            h.seed(`on_${i}`, `成员${i}`, false, 1000);
        const before = h.members.get('on_100');
        await h.directory.changeMembers(
            'oc_group',
            [{ unionId: 'on_new', name: '张若' }],
            false,
            new Date(2000),
        );
        expect(h.profiles.get('on_new')?.name).toBe('张若');
        expect(h.members.get('on_new')?.hasLeft).toBe(false);
        expect(h.members.get('on_100')).toEqual(before);
        expect(h.calls.filter((c) => c.startsWith('contact:'))).toEqual([]);
    });

    it('writes shared profiles in stable order and does not overwrite names on leave', async () => {
        const h = rig();
        await h.directory.changeMembers(
            'oc_group',
            [
                { unionId: 'on_z', name: '后' },
                { unionId: 'on_a', name: '前' },
            ],
            false,
            new Date(1000),
        );
        expect(h.calls.filter((c) => c.startsWith('profile:'))).toEqual([
            'profile:on_a',
            'profile:on_z',
        ]);
        await h.directory.changeMembers(
            'oc_group',
            [{ unionId: 'on_a', name: '退群旧名' }],
            true,
            new Date(2000),
        );
        expect(h.profiles.get('on_a')?.name).toBe('前');
    });

    it('restores an old left state from a newer real message', async () => {
        const h = rig();
        h.seed('on_new', '张若', true, 1000);
        expect(await h.directory.ensureSender(event())).toBe(true);
        expect(h.members.get('on_new')).toMatchObject({
            hasLeft: false,
            updatedAt: new Date(2000),
        });
        expect(h.calls).not.toContain('contact:on_new');
    });

    it('does not revive a newer leave from an old message', async () => {
        const h = rig();
        h.seed('on_new', '张若', true, 3000);
        await h.directory.ensureSender(event());
        expect(h.members.get('on_new')).toMatchObject({
            hasLeft: true,
            updatedAt: new Date(3000),
        });
    });

    it('advances complete active senders to resist a late arriving older leave', async () => {
        const h = rig();
        h.seed('on_new', '张若', false, 1000);
        expect(await h.directory.ensureSender(event('on_new', 3000))).toBe(
            false,
        );
        await h.directory.changeMembers(
            'oc_group',
            [{ unionId: 'on_new', name: '张若' }],
            true,
            new Date(2000),
        );
        expect(h.members.get('on_new')).toMatchObject({
            hasLeft: false,
            updatedAt: new Date(3000),
        });
        expect(h.calls).not.toContain('contact:on_new');
    });

    it('inserts unknown leave tombstones and gives leave priority at equal milliseconds', async () => {
        const h = rig();
        await h.directory.changeMembers(
            'oc_group',
            [{ unionId: 'on_new', name: '张若' }],
            true,
            new Date(2000),
        );
        expect(h.profiles.size).toBe(0);
        await h.directory.changeMembers(
            'oc_group',
            [{ unionId: 'on_new', name: '张若' }],
            false,
            new Date(2000),
        );
        await h.directory.ensureSender(event());
        expect(h.members.get('on_new')?.hasLeft).toBe(true);
        await h.directory.ensureSender(event('on_new', 3000));
        await h.directory.changeMembers(
            'oc_group',
            [{ unionId: 'on_new', name: '张若' }],
            true,
            new Date(3000),
        );
        expect(h.members.get('on_new')?.hasLeft).toBe(true);
    });

    it('commits message evidence before a contact permission failure', async () => {
        const h = rig();
        h.seed('on_new', null, true, 1000);
        h.api.user = async () => {
            h.calls.push('contact:denied');
            throw new Error('no user authority');
        };
        await expect(h.directory.ensureSender(event())).rejects.toThrow(
            'no user authority',
        );
        expect(h.members.get('on_new')).toMatchObject({
            hasLeft: false,
            updatedAt: new Date(2000),
        });
        expect(h.profiles.size).toBe(0);
        expect(h.calls.indexOf('commit')).toBeLessThan(
            h.calls.indexOf('contact:denied'),
        );
    });

    it('fills a missing profile even when an older message cannot restore membership', async () => {
        const h = rig();
        h.seed('on_new', null, true, 3000);
        expect(await h.directory.ensureSender(event())).toBe(true);
        expect(h.profiles.get('on_new')?.name).toBe('新人');
        expect(h.members.get('on_new')?.hasLeft).toBe(true);
    });

    it('rechecks names before committing concurrent contact lookups', async () => {
        const h = rig();
        expect(
            await Promise.all([
                h.directory.ensureSender(event()),
                h.directory.ensureSender(event()),
            ]),
        ).toEqual([true, true]);
        expect(h.calls.filter((c) => c === 'profile:on_new')).toHaveLength(1);
    });

    it('private messages only fetch missing profiles and skip known profiles', async () => {
        const h = rig();
        expect(
            await h.directory.ensureSender(event('on_new', 2000, 'p2p')),
        ).toBe(true);
        expect(
            await h.directory.ensureSender(event('on_new', 3000, 'p2p')),
        ).toBe(false);
        expect(h.members.size).toBe(0);
        expect(h.calls.filter((c) => c === 'contact:on_new')).toHaveLength(1);
    });

    it('skips app senders and known bot identities', async () => {
        const h = rig();
        expect(
            await h.directory.ensureSender(
                event('on_new', 2000, 'group', 'app'),
            ),
        ).toBe(false);
        expect(await h.directory.ensureSender(event('on_bot'))).toBe(false);
        expect(h.calls).toEqual([]);
    });

    it('rejects invalid evidence timestamps before changing membership', async () => {
        const h = rig();
        await expect(
            h.directory.ensureSender(event('on_new', NaN)),
        ).rejects.toThrow('time');
        await expect(
            h.directory.changeMembers(
                'oc_group',
                [{ unionId: 'on_new', name: '新人' }],
                false,
                new Date(NaN),
            ),
        ).rejects.toThrow('time');
        expect(h.members.size).toBe(0);
    });

    it('produces real outbound mentions after join/message facts, keeping a departed user as text', async () => {
        const h = rig();
        h.seed('on_back', '张若', true, 1000);
        h.seed('on_gone', '离群用户', false, 1000);
        const resolve = createLarkMentionResolver({
            aliases: () => [],
            roster: {
                entries: async () =>
                    [...h.members.values()].flatMap((member) => {
                        const profile = h.profiles.get(member.unionId);
                        return profile
                            ? [
                                  {
                                      unionId: member.unionId,
                                      name: profile.name,
                                      hasLeft: member.hasLeft,
                                  },
                              ]
                            : [];
                    }),
            },
        });
        await h.directory.ensureSender(event('on_back'));
        await h.directory.changeMembers(
            'oc_group',
            [{ unionId: 'on_new', name: '新人' }],
            false,
            new Date(2000),
        );
        await h.directory.changeMembers(
            'oc_group',
            [{ unionId: 'on_gone', name: '离群用户' }],
            true,
            new Date(2000),
        );
        expect(await resolve('@张若 @新人 @离群用户', 'oc_group')).toBe(
            '<at user_id="on_back">张若</at> <at user_id="on_new">新人</at> @离群用户',
        );
    });
});

it('does not replace a current name from a rejected older join, but can fill a missing name', async () => {
    const h = rig();
    h.seed('on_current', '新名字', false, 3000);
    h.seed('on_missing', null, true, 3000);
    await h.directory.changeMembers(
        'oc_group',
        [
            { unionId: 'on_current', name: '旧名字' },
            { unionId: 'on_missing', name: '已离群用户' },
        ],
        false,
        new Date(2000),
    );
    expect(h.profiles.get('on_current')?.name).toBe('新名字');
    expect(h.profiles.get('on_missing')?.name).toBe('已离群用户');
    expect(h.members.get('on_missing')?.hasLeft).toBe(true);
});

it('requests a projection refresh when a known profile gains a missing membership', async () => {
    const h = rig();
    h.profiles.set('on_new', { unionId: 'on_new', name: '新人' });
    expect(await h.directory.ensureSender(event())).toBe(true);
    expect(h.calls).not.toContain('contact:on_new');
});

it('allows same-chat membership events during a slow contact lookup and preserves their fresh name', async () => {
    const h = rig();
    let started!: () => void;
    const contactStarted = new Promise<void>((resolve) => {
        started = resolve;
    });
    let finish!: (profile: LarkDirectoryProfile) => void;
    const pendingContact = new Promise<LarkDirectoryProfile>((resolve) => {
        finish = resolve;
    });
    h.api.user = async () => {
        started();
        return pendingContact;
    };
    const message = h.directory.ensureSender(event());
    await contactStarted;
    const membership = h.directory.changeMembers(
        'oc_group',
        [{ unionId: 'on_new', name: '入群新名字' }],
        false,
        new Date(3000),
    );
    let timer: ReturnType<typeof setTimeout> | undefined;
    let completedBeforeContact: boolean;
    try {
        completedBeforeContact = await Promise.race([
            membership.then(() => true),
            new Promise<boolean>((resolve) => {
                timer = setTimeout(() => resolve(false), 30);
            }),
        ]);
    } finally {
        clearTimeout(timer);
        finish({ unionId: 'on_new', name: '联系人旧名字' });
        await Promise.all([message, membership]);
    }
    expect(completedBeforeContact!).toBe(true);
    expect(h.profiles.get('on_new')?.name).toBe('入群新名字');
    expect(h.members.get('on_new')?.updatedAt).toEqual(new Date(3000));
    expect(h.calls.filter((c) => c === 'profile:on_new')).toHaveLength(1);
});

it('keeps a nonempty profile name when membership events from different chats disagree', async () => {
    const h = rig();
    await h.directory.changeMembers(
        'oc_group',
        [{ unionId: 'on_new', name: '现有名字' }],
        false,
        new Date(1000),
    );
    await h.directory.changeMembers(
        'oc_other',
        [{ unionId: 'on_new', name: '另一群事件名字' }],
        false,
        new Date(2000),
    );
    await h.directory.changeMembers(
        'oc_third',
        [{ unionId: 'on_new', name: '晚到旧名字' }],
        false,
        new Date(500),
    );
    expect(h.profiles.get('on_new')?.name).toBe('现有名字');
});

it('fills a blank profile from a join older than already committed message evidence', async () => {
    const h = rig();
    h.seed('on_new', '  ', false, 3000);
    await h.directory.changeMembers(
        'oc_group',
        [{ unionId: 'on_new', name: '新人名字' }],
        false,
        new Date(2000),
    );
    expect(h.profiles.get('on_new')?.name).toBe('新人名字');
    expect(h.members.get('on_new')?.updatedAt).toEqual(new Date(3000));
});
