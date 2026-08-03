import { Column, Entity, Index, PrimaryColumn } from 'typeorm';

/**
 * 同一个人在每个飞书应用下各有一个 open_id，主键是 (app_id, open_id)。
 * common_user_id 是这张表通往公共层的桥：群里"这条消息是不是冲 bot 来的"只比
 * common id，不比渠道裸 id。
 */
@Entity('lark_user_open_id')
@Index('idx_lark_user_open_id_common_user_id', ['commonUserId'])
export class LarkUserOpenId {
    @PrimaryColumn({ name: 'app_id', type: 'varchar' })
    appId!: string;

    @PrimaryColumn({ name: 'open_id', type: 'varchar' })
    openId!: string;

    @Column({ name: 'union_id', type: 'varchar', nullable: true })
    unionId?: string;

    @Column({ type: 'varchar' })
    name!: string;

    @Column({ name: 'common_user_id', type: 'uuid', nullable: true })
    commonUserId?: string;
}
