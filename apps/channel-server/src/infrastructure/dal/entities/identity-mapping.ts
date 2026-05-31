import { Entity, PrimaryColumn, Column, Unique, CreateDateColumn } from 'typeorm';

// 三类身份映射表。结构完全相同：(channel, channel_*_id) 复合唯一，
// internal_*_id 全局唯一。三张表对应 IdentityKind 的三个独立命名空间
// user / conversation / message——拆三张表而不是一张带 kind 列，是为了让
// 各身份维度的外键/索引各自独立、迁移与回填互不耦合。
//
// internal_*_id 用 UUIDv7（小写，PG 原生 uuid 列；见 db-identity-resolver.ts
// 选型说明）。两层表（底层 user/conversation/message + 每平台 lark_* 表）的
// 拆分属于 C2 迁移：要把 250 万 conversation_messages + Qdrant 一起折进去，
// 在真实数据上做一次，不在 C1 预建空表。C1 只把这张身份映射做对到目标世界
// （UUIDv7 + conversation 命名）。

@Entity('identity_user')
@Unique('uq_identity_user_channel', ['channel', 'channel_user_id'])
export class IdentityUser {
    @PrimaryColumn({ type: 'uuid' })
    internal_user_id!: string;

    @Column({ type: 'varchar', length: 64 })
    channel!: string;

    @Column({ type: 'varchar', length: 256 })
    channel_user_id!: string;

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;
}

@Entity('identity_conversation')
@Unique('uq_identity_conversation_channel', ['channel', 'channel_conversation_id'])
export class IdentityConversation {
    @PrimaryColumn({ type: 'uuid' })
    internal_conversation_id!: string;

    @Column({ type: 'varchar', length: 64 })
    channel!: string;

    @Column({ type: 'varchar', length: 256 })
    channel_conversation_id!: string;

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;
}

@Entity('identity_message')
@Unique('uq_identity_message_channel', ['channel', 'channel_message_id'])
export class IdentityMessage {
    @PrimaryColumn({ type: 'uuid' })
    internal_message_id!: string;

    @Column({ type: 'varchar', length: 64 })
    channel!: string;

    @Column({ type: 'varchar', length: 256 })
    channel_message_id!: string;

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;
}
