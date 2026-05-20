import { Entity, PrimaryColumn, Column, Unique, CreateDateColumn } from 'typeorm';

// 三类身份映射表（T5）。结构完全相同：(channel, channel_*_id) 复合唯一，
// internal_*_id 全局唯一。三张表对应 IdentityKind 的三个独立命名空间
// user / chat / message——拆三张表而不是一张带 kind 列，是为了让 7 张旧表
// 一刀切迁移时各身份维度的外键/索引各自独立、迁移与回填互不耦合。
//
// internal_*_id 用 ULID 字符串（见 db-identity-resolver.ts 选型说明）。

@Entity('identity_user')
@Unique('uq_identity_user_channel', ['channel', 'channel_user_id'])
export class IdentityUser {
    @PrimaryColumn({ type: 'varchar', length: 26 })
    internal_user_id!: string;

    @Column({ type: 'varchar', length: 64 })
    channel!: string;

    @Column({ type: 'varchar', length: 256 })
    channel_user_id!: string;

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;
}

@Entity('identity_chat')
@Unique('uq_identity_chat_channel', ['channel', 'channel_chat_id'])
export class IdentityChat {
    @PrimaryColumn({ type: 'varchar', length: 26 })
    internal_chat_id!: string;

    @Column({ type: 'varchar', length: 64 })
    channel!: string;

    @Column({ type: 'varchar', length: 256 })
    channel_chat_id!: string;

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;
}

@Entity('identity_message')
@Unique('uq_identity_message_channel', ['channel', 'channel_message_id'])
export class IdentityMessage {
    @PrimaryColumn({ type: 'varchar', length: 26 })
    internal_message_id!: string;

    @Column({ type: 'varchar', length: 64 })
    channel!: string;

    @Column({ type: 'varchar', length: 256 })
    channel_message_id!: string;

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;
}
