import { Entity, PrimaryColumn, Column } from 'typeorm';

@Entity('lark_card_context')
export class LarkCardContext {
    @PrimaryColumn()
    card_id!: string;

    @Column()
    message_id!: string;

    @Column()
    chat_id!: string;

    @Column('int')
    sequence!: number;

    @Column('timestamp')
    created_at!: Date;

    @Column('timestamp')
    last_updated!: Date;
}
