import {
    Entity,
    Column,
    PrimaryGeneratedColumn,
    CreateDateColumn,
    UpdateDateColumn,
} from 'typeorm';

@Entity('user_group_binding')
export class UserGroupBinding {
    @PrimaryGeneratedColumn()
    id!: number;

    @Column({ name: 'user_union_id' })
    userUnionId!: string;

    @Column({ name: 'chat_id' })
    chatId!: string;

    @Column({ name: 'is_active', default: true })
    isActive: boolean = true;

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;

    @UpdateDateColumn({ name: 'updated_at' })
    updatedAt!: Date;
}
