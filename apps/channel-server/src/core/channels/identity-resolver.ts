// channel 作用域身份模型。把"channel 内 ID"翻译成 channel 无关的全局内部 ID。
//
// 进站（channel-server 收到消息）走 resolve：(channel, channelId) -> 全局 ID,
// 不存在则原子分配；出站（chat-response-worker 发回复）走 toChannel 反查。
// 三类身份（user / chat / message）是三个互相独立的命名空间。
//
// 这里的 InMemoryIdentityResolver 是 T1 的领域实现，用来把契约钉死。
// 真正的 DB 持久化、飞书历史一刀切迁移是 T5 的事，会换一个走映射表的实现，
// 但必须满足这里测试覆盖的同一套契约。

export type IdentityKind = 'user' | 'chat' | 'message';

export interface ChannelRef {
    channel: string;
    channelId: string;
}

export interface IdentityResolver {
    // 正查：channel 内 ID -> 全局 ID。幂等；首次出现则分配一个全局唯一 ID。
    resolve(kind: IdentityKind, channel: string, channelId: string): Promise<string>;

    // 反查：全局 ID -> (channel, channelId)。查不到必须抛错，不能静默放过。
    toChannel(kind: IdentityKind, internalId: string): Promise<ChannelRef>;
}

export class IdentityNotFoundError extends Error {
    constructor(kind: IdentityKind, internalId: string) {
        super(`no ${kind} identity mapping for internal id "${internalId}"`);
        this.name = 'IdentityNotFoundError';
    }
}

// 用嵌套 Map（channel -> channelId -> internalId）而不是把 channel 和
// channelId 拼成一个字符串 key。两者都来自外部，任何分隔符都可能出现在其中
// 造成歧义碰撞；嵌套结构从根上消除这个问题，也更贴近 T5 DB 映射表
// (channel, channel_id) 复合唯一键的语义。
interface KindMaps {
    forward: Map<string, Map<string, string>>;
    backward: Map<string, ChannelRef>;
}

export class InMemoryIdentityResolver implements IdentityResolver {
    private readonly maps: Record<IdentityKind, KindMaps> = {
        user: { forward: new Map(), backward: new Map() },
        chat: { forward: new Map(), backward: new Map() },
        message: { forward: new Map(), backward: new Map() },
    };

    async resolve(kind: IdentityKind, channel: string, channelId: string): Promise<string> {
        const m = this.maps[kind];
        let byChannel = m.forward.get(channel);
        if (byChannel === undefined) {
            byChannel = new Map<string, string>();
            m.forward.set(channel, byChannel);
        }
        const existing = byChannel.get(channelId);
        if (existing !== undefined) return existing;

        const internalId = crypto.randomUUID();
        byChannel.set(channelId, internalId);
        m.backward.set(internalId, { channel, channelId });
        return internalId;
    }

    async toChannel(kind: IdentityKind, internalId: string): Promise<ChannelRef> {
        const ref = this.maps[kind].backward.get(internalId);
        if (ref === undefined) throw new IdentityNotFoundError(kind, internalId);
        return ref;
    }
}
