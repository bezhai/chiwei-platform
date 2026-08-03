import { Column, CreateDateColumn, Entity, PrimaryColumn, UpdateDateColumn } from 'typeorm';

/** 飞书表情 key → 文本描述。由每小时的同步任务全量覆写。 */
@Entity('lark_emoji')
export class LarkEmoji {
    @PrimaryColumn({ type: 'varchar', length: 100 })
    key!: string;

    @Column({ type: 'varchar', length: 500 })
    text!: string;

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;

    @UpdateDateColumn({ name: 'updated_at' })
    updatedAt!: Date;
}
