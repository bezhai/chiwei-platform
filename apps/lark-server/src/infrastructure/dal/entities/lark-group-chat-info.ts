import { Entity, PrimaryColumn, Column, OneToOne, JoinColumn } from 'typeorm';
import { LarkBaseChatInfo } from './lark-base-chat-info';

@Entity('lark_group_chat_info')
export class LarkGroupChatInfo {
    @PrimaryColumn()
    chat_id!: string; // 对话 ID，与 LarkBaseChatInfo 共享

    @OneToOne(() => LarkBaseChatInfo, { cascade: true })
    @JoinColumn({ name: 'chat_id' })
    baseChatInfo?: LarkBaseChatInfo; // 关联 LarkBaseChatInfo 实体

    @Column()
    name!: string; // 群聊名称

    @Column({ type: 'text', nullable: true })
    avatar?: string; // 群聊头像

    @Column({ type: 'text', nullable: true })
    description?: string; // 群聊描述

    @Column('text', { array: true, nullable: true })
    user_manager_id_list?: string[]; // 群聊管理员 ID 列表

    @Column({ type: 'varchar', length: 255, nullable: true })
    chat_tag?: string; // 群标签

    @Column({ type: 'varchar', length: 10, nullable: true })
    group_message_type?: 'chat' | 'thread'; // 群聊消息类型

    @Column({ type: 'varchar', length: 20 })
    chat_status!: 'normal' | 'dissolved' | 'dissolved_save'; // 群聊状态

    @Column({ type: 'varchar', length: 20, nullable: true })
    download_has_permission_setting?: 'all_members' | 'not_anyone'; // 下载权限设置

    @Column({ type: 'int' })
    user_count!: number; // 群聊人数

    @Column({ type: 'boolean', default: false })
    is_leave?: boolean; // 是否在群聊中
}
