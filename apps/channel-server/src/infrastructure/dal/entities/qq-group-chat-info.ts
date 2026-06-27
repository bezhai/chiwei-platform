import { Column, Entity, Index, PrimaryColumn } from 'typeorm';

// QQ 会话 id ↔ common_conversation 映射（对飞书 LarkBaseChatInfo）。
//
// 同时承载私聊（scope='direct'）与群聊（scope='group'）两种会话，正如
// LarkBaseChatInfo 同时存 p2p 和 group。common_conversation_id 唯一索引：
// 出站反查 common → QQ 会话裸 id 走它。
@Entity('qq_group_chat_info')
@Index('uq_qq_group_chat_info_common_conversation_id', ['common_conversation_id'], {
    unique: true,
})
export class QqGroupChatInfo {
    @PrimaryColumn({ name: 'conversation_id', type: 'varchar', length: 256 })
    conversation_id!: string;

    @Column({ type: 'varchar', length: 16 })
    scope!: 'direct' | 'group';

    @Column({ name: 'bot_name', type: 'varchar', length: 64, nullable: true })
    bot_name?: string;

    @Column({ name: 'common_conversation_id', type: 'uuid', nullable: true })
    common_conversation_id?: string;
}
