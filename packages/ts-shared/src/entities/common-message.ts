import { Column, CreateDateColumn, Entity, Index, PrimaryColumn } from 'typeorm';

export interface CommonMessageContent {
    type: string;
    [key: string]: unknown;
}

@Entity('common_message')
@Index('idx_common_message_conversation_time', ['common_conversation_id', 'event_time'])
@Index('idx_common_message_user_time', ['common_user_id', 'event_time'])
@Index('idx_common_message_response_id', ['response_id'])
export class CommonMessage {
    @PrimaryColumn({ type: 'uuid' })
    common_message_id!: string;

    @Column({ type: 'varchar', length: 64 })
    channel!: string;

    @Column({ type: 'uuid' })
    common_conversation_id!: string;

    @Column({ type: 'uuid', nullable: true })
    common_user_id?: string;

    @Column({ type: 'varchar', length: 256, nullable: true })
    sender_display_name?: string;

    @Column({ type: 'varchar', length: 20 })
    role!: string;

    @Column({ type: 'jsonb', default: () => "'[]'::jsonb" })
    content!: CommonMessageContent[];

    @Column({ type: 'text', nullable: true })
    content_text?: string;

    @Column({ type: 'uuid', nullable: true })
    common_root_message_id?: string;

    @Column({ type: 'uuid', nullable: true })
    common_reply_message_id?: string;

    /**
     * 这条消息点了谁的名，按公共层 id。
     *
     * 存 common_user_id 而不是渠道那边的 union_id / open_id：读它的人（agent-service
     * 判"群里叫的是不是我"）只认公共层 id，而渠道裸 id 一律不上浮到这一层。
     *
     * **NULL 和 `[]` 是两件事。** NULL = 没人算过这条消息（加列之前的存量行、QQ 的
     * 行、飞书新写入方上线之前的行）；`[]` = 算过，确实谁都没点。读的一侧把 NULL 当
     * "不知道"，也就是不算被点名 —— 这是唯一安全的方向。所以这一列既不能 NOT NULL，
     * 也不能给默认值：给了就把"没算过"和"算过没人"合并了，而且合并之后再也分不开。
     */
    @Column({ type: 'uuid', array: true, nullable: true })
    mentioned_common_user_ids?: string[];

    @Column({ type: 'varchar', length: 16 })
    scope!: string;

    @Column({ type: 'varchar', length: 30, nullable: true })
    message_type?: string;

    @Column({ type: 'varchar', length: 50, nullable: true })
    bot_name?: string;

    @Column({ type: 'varchar', length: 100, nullable: true })
    response_id?: string;

    @Column({ type: 'bigint' })
    event_time!: string;

    @CreateDateColumn({ name: 'created_at', type: 'timestamp' })
    created_at!: Date;
}
