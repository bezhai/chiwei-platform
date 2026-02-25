import { Entity, PrimaryColumn, Column, CreateDateColumn, UpdateDateColumn } from 'typeorm';

@Entity('lark_emoji')
export class LarkEmoji {
    @PrimaryColumn({ type: 'varchar', length: 100 })
    key!: string; // emoji的唯一标识符

    @Column({ type: 'varchar', length: 500 })
    text!: string; // emoji的文本描述

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;

    @UpdateDateColumn({ name: 'updated_at' })
    updatedAt!: Date;
}

