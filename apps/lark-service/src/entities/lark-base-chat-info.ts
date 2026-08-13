import { Column, Entity, Index, PrimaryColumn } from 'typeorm';

/** 会话的基础信息与权限/灰度配置。群聊的额外字段在 lark_group_chat_info。 */
@Entity('lark_base_chat_info')
@Index('uq_lark_base_chat_info_common_conversation_id', ['common_conversation_id'], {
    unique: true,
})
export class LarkBaseChatInfo {
    @PrimaryColumn()
    chat_id!: string;

    @Column({ type: 'varchar', length: 10 })
    chat_mode!: 'group' | 'topic' | 'p2p';

    @Column({ type: 'jsonb', nullable: true })
    permission_config?: {
        allow_send_message?: boolean;
        allow_send_pixiv_image?: boolean;
        open_repeat_message?: boolean;
        allow_send_limit_photo?: boolean;
        can_access_restricted_models?: boolean;
        can_access_restricted_prompts?: boolean;
        new_permission?: boolean;
        is_canary?: boolean;
    };

    // 类型需含 null：清空灰度配置时写的是 null，TypeORM 的 partial update 会拒绝
    // 一个不含 null 的类型。
    @Column({ type: 'jsonb', nullable: true })
    gray_config?: Record<string, string> | null;

    @Column({ type: 'uuid', nullable: true })
    common_conversation_id?: string;
}
