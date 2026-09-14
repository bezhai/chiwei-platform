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

function event(unionId = 'on_new', chatType = 'group', senderType = 'user'): LarkMessageEvent {
    return {
        sender: {
            sender_type: senderType,
            sender_id: { union_id: unionId, open_id: 'ou_new' },
        },
        message: {
            message_id: 'om_old',
            create_time: '1',
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
    const evidence = new Set<string>();
    const calls: string[] = [];
    let current: LarkDirectoryProfile[] = [];
    let failList = false;
    const tables: LarkDirectoryTables = {
        async profile(id) {
            return profiles.get(id) ?? null;
        },
        async member(_chatId, id) {
            return members.get(id) ?? null;
        },
        async members() {
            return [...members.values()];
        },
        async humanUnionIds() {
            return [...evidence];
        },
        async saveProfile(profile) {
            calls.push(`profile:${profile.unionId}`);
            profiles.set(profile.unionId, profile);
        },
        async applyMembers(_chatId, present, left) {
            calls.push('apply');
            for (const person of present)
                members.set(person.unionId, { ...person, hasLeft: false });
            for (const id of left) {
                const old = members.get(id)!;
                members.set(id, { ...old, hasLeft: true });
            }
        },
    };
    const store: LarkDirectoryStore = {
        ...tables,
        async withChatLock(_chatId, run) {
            calls.push('lock');
            try {
                return await run(tables);
            } finally {
                calls.push('unlock');
            }
        },
    };
    const api: LarkDirectoryApi = {
        async members() {
            calls.push('fetch');
            if (failList) throw new Error('API failed');
            return current;
        },
        async user(id) {
            calls.push(`contact:${id}`);
            return { unionId: id, name: '历史用户' };
        },
    };
    return {
        profiles,
        members,
        evidence,
        calls,
        store,
        api,
        setCurrent(v: LarkDirectoryProfile[]) {
            current = v;
        },
        failList() {
            failList = true;
        },
        directory: createLarkDirectory({ store, api, botUnionIds: ['on_bot'] }),
    };
}

describe('LarkDirectory', () => {
    it('previews missing users, returning users, renamed users, and proven humans who left without writing', async () => {
        const h = rig();
        h.members.set('on_back', {
            unionId: 'on_back',
            name: '旧名',
            hasLeft: true,
        });
        h.members.set('on_gone', {
            unionId: 'on_gone',
            name: '离群',
            hasLeft: false,
        });
        h.members.set('on_unknown', {
            unionId: 'on_unknown',
            name: '无法分类',
            hasLeft: false,
        });
        h.members.set('on_bot', {
            unionId: 'on_bot',
            name: '机器人',
            hasLeft: false,
        });
        h.profiles.set('on_back', { unionId: 'on_back', name: '旧名' });
        h.evidence.add('on_gone');
        h.setCurrent([
            { unionId: 'on_back', name: '新名' },
            { unionId: 'on_new', name: '新人' },
        ]);
        const result = await h.directory.sync('oc_group', {
            preview: true,
            humanUnionIds: ['on_bot'],
        });
        expect(result.joined.map((x) => x.unionId)).toEqual(['on_back', 'on_new']);
        expect(result.left).toEqual([{ unionId: 'on_gone', name: '离群' }]);
        expect(result.renamed).toContainEqual({
            unionId: 'on_back',
            before: '旧名',
            after: '新名',
        });
        expect(h.calls).toEqual(['lock', 'fetch', 'unlock']);
        expect(h.members.get('on_back')!.hasLeft).toBe(true);
    });

    it('commits fresh names and membership inside the fetch lock, preserving unclassified rows', async () => {
        const h = rig();
        h.members.set('on_gone', {
            unionId: 'on_gone',
            name: '离群',
            hasLeft: false,
        });
        h.members.set('on_unknown', {
            unionId: 'on_unknown',
            name: '未知',
            hasLeft: false,
        });
        h.setCurrent([{ unionId: 'on_new', name: '新人' }]);
        await h.directory.sync('oc_group', {
            preview: false,
            humanUnionIds: ['on_gone'],
        });
        expect(h.calls).toEqual(['lock', 'fetch', 'profile:on_new', 'apply', 'unlock']);
        expect(h.members.get('on_gone')!.hasLeft).toBe(true);
        expect(h.members.get('on_unknown')!.hasLeft).toBe(false);
        expect(h.profiles.get('on_new')!.name).toBe('新人');
    });

    it('does not write anything after a failed fetch', async () => {
        const h = rig();
        h.failList();
        await expect(
            h.directory.sync('oc_group', { preview: false, humanUnionIds: [] }),
        ).rejects.toThrow('API failed');
        expect(h.calls).toEqual(['lock', 'fetch', 'unlock']);
    });

    it('writes shared user profiles in a stable order across different chat snapshots', async () => {
        const h = rig();
        h.setCurrent([
            { unionId: 'on_z', name: '后' },
            { unionId: 'on_a', name: '前' },
        ]);
        await h.directory.sync('oc_group', { preview: false, humanUnionIds: [] });
        expect(h.calls.filter((call) => call.startsWith('profile:'))).toEqual([
            'profile:on_a',
            'profile:on_z',
        ]);
    });

    it('skips app senders and complete active humans without API calls', async () => {
        const h = rig();
        expect(await h.directory.ensureSender(event('on_new', 'group', 'app'))).toBe(false);
        h.profiles.set('on_new', { unionId: 'on_new', name: '新人' });
        h.members.set('on_new', {
            unionId: 'on_new',
            name: '新人',
            hasLeft: false,
        });
        expect(await h.directory.ensureSender(event())).toBe(false);
        expect(h.calls).toEqual([]);
    });

    it('repairs a missing sender from the current roster', async () => {
        const h = rig();
        h.setCurrent([{ unionId: 'on_new', name: '张若' }]);
        expect(await h.directory.ensureSender(event())).toBe(true);
        expect(h.members.get('on_new')!.hasLeft).toBe(false);
        expect(h.profiles.get('on_new')!.name).toBe('张若');
        expect(h.calls).not.toContain('contact:on_new');
    });

    it('does not restore a historical sender absent from the current roster, but retrieves a missing profile', async () => {
        const h = rig();
        h.members.set('on_new', { unionId: 'on_new', name: null, hasLeft: false });
        expect(await h.directory.ensureSender(event())).toBe(true);
        expect(h.members.get('on_new')!.hasLeft).toBe(true);
        expect(h.profiles.get('on_new')!.name).toBe('历史用户');
        expect(h.calls).toContain('contact:on_new');
    });

    it('private messages only refresh the profile', async () => {
        const h = rig();
        expect(await h.directory.ensureSender(event('on_new', 'p2p'))).toBe(true);
        expect(h.calls).toEqual(['contact:on_new', 'profile:on_new']);
        expect(h.members.size).toBe(0);
    });

    it('propagates contact permission errors without manufacturing a profile', async () => {
        const h = rig();
        h.api.user = async () => {
            throw new Error('no user authority');
        };
        await expect(h.directory.ensureSender(event('on_new', 'p2p'))).rejects.toThrow(
            'no user authority',
        );
        expect(h.profiles.size).toBe(0);
    });

    it('turns a returning user and a newcomer into real outbound mentions, keeping an absent human as text', async () => {
        const h = rig();
        h.profiles.set('on_back', { unionId: 'on_back', name: '张若' });
        h.members.set('on_back', {
            unionId: 'on_back',
            name: '张若',
            hasLeft: true,
        });
        h.profiles.set('on_gone', { unionId: 'on_gone', name: '离群用户' });
        h.members.set('on_gone', {
            unionId: 'on_gone',
            name: '离群用户',
            hasLeft: false,
        });
        h.evidence.add('on_gone');
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
        const text = '@张若 @新人 @离群用户';
        expect(await resolve(text, 'oc_group')).toBe(
            '@张若 @新人 <at user_id="on_gone">离群用户</at>',
        );
        h.setCurrent([
            { unionId: 'on_back', name: '张若' },
            { unionId: 'on_new', name: '新人' },
        ]);
        await h.directory.sync('oc_group', { preview: false, humanUnionIds: [] });
        expect(await resolve(text, 'oc_group')).toBe(
            '<at user_id="on_back">张若</at> <at user_id="on_new">新人</at> @离群用户',
        );
    });
});

it('concurrent missing senders recheck after acquiring the lock instead of fetching twice', async () => {
    const h = rig();
    h.setCurrent([{ unionId: 'on_new', name: '新人' }]);
    let tail: Promise<unknown> = Promise.resolve();
    const lock = h.store.withChatLock.bind(h.store);
    h.store.withChatLock = (chat, run) => {
        const next = tail.then(() => lock(chat, run));
        tail = next.catch(() => {});
        return next;
    };
    const results = await Promise.all([
        h.directory.ensureSender(event()),
        h.directory.ensureSender(event()),
    ]);
    expect(results).toEqual([true, true]);
    expect(h.calls.filter((c) => c === 'fetch')).toHaveLength(1);
});
