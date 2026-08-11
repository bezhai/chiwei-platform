import {
    Column,
    CreateDateColumn,
    Entity,
    PrimaryGeneratedColumn,
    UpdateDateColumn,
} from 'typeorm';

/**
 * 管理员用 `/bind` 把一个人绑在一个群上：他退群就自动拉回来。`/unbind` 解绑。
 *
 * 表名里没有 lark 前缀，但它是**飞书独占**的 —— 全仓的读写点只有那三处飞书功能
 * （`/bind`、`/unbind`、退群自动拉回），列里存的也是飞书的 union_id 和 chat_id。
 *
 * **(user_union_id, chat_id) 上没有唯一约束。** 所以"有没有绑过"只能先读再写，两个
 * 管理员同时敲 `/bind` 会留下两行。这是既有形态，登记在案不在这一批改 —— 补约束要
 * 先清历史重复行，属于 schema 变更。
 */
@Entity('user_group_binding')
export class UserGroupBinding {
    @PrimaryGeneratedColumn()
    id!: number;

    @Column({ name: 'user_union_id' })
    userUnionId!: string;

    @Column({ name: 'chat_id' })
    chatId!: string;

    /** 解绑是软删：行留着，只把这一位关掉。 */
    @Column({ name: 'is_active', default: true })
    isActive: boolean = true;

    @CreateDateColumn({ name: 'created_at' })
    createdAt!: Date;

    @UpdateDateColumn({ name: 'updated_at' })
    updatedAt!: Date;
}
