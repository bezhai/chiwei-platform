import { Column, Entity, JoinColumn, OneToOne, PrimaryColumn } from 'typeorm';

import { LarkBaseChatInfo } from './lark-base-chat-info';

/** 群聊独有的信息。与 lark_base_chat_info 共用主键 chat_id，1:1。 */
@Entity('lark_group_chat_info')
export class LarkGroupChatInfo {
    @PrimaryColumn()
    chat_id!: string;

    @OneToOne(() => LarkBaseChatInfo, { cascade: true })
    @JoinColumn({ name: 'chat_id' })
    baseChatInfo?: LarkBaseChatInfo;

    @Column()
    name!: string;

    @Column({ type: 'text', nullable: true })
    avatar?: string;

    @Column({ type: 'text', nullable: true })
    description?: string;

    @Column('text', { array: true, nullable: true })
    user_manager_id_list?: string[];

    @Column({ type: 'varchar', length: 255, nullable: true })
    chat_tag?: string;

    @Column({ type: 'varchar', length: 10, nullable: true })
    group_message_type?: 'chat' | 'thread';

    @Column({ type: 'varchar', length: 20 })
    chat_status!: 'normal' | 'dissolved' | 'dissolved_save';

    @Column({ type: 'varchar', length: 20, nullable: true })
    download_has_permission_setting?: 'all_members' | 'not_anyone';

    @Column({ type: 'int' })
    user_count!: number;

    @Column({ type: 'boolean', default: false })
    is_leave?: boolean;
}
