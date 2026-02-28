import { Entity, PrimaryColumn, Column } from 'typeorm';

@Entity('lark_group_chat_info')
export class LarkGroupChatInfo {
    @PrimaryColumn()
    chat_id!: string;

    @Column()
    name!: string;
}
