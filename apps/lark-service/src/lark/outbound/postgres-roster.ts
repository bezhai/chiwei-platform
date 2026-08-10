// LarkGroupRoster 的真身。一条语句，不做判断。
//
// 名字不在 lark_group_member 上 —— 那张表只记"谁在哪个群、是什么身份"，名字在
// lark_user 上按 union_id 存。所以读花名册必然是一次 join。
//
// **不过滤退群的人。** 谁能被 @ 是判断，判断在 mentions.ts 里做。在这里少读一行，
// 那边的分支就永远走不到，测试也就守不住任何东西。

import type { DataSource } from 'typeorm';

import { LarkGroupMember } from '../../entities/lark-group-member';
import { LarkUser } from '../../entities/lark-user';
import type { LarkGroupRoster, LarkRosterEntry } from './mentions';

interface RosterRow {
    union_id: string;
    name: string;
    is_leave: boolean | null;
}

export function postgresLarkGroupRoster(dataSource: DataSource): LarkGroupRoster {
    return {
        async entries(chatId): Promise<readonly LarkRosterEntry[]> {
            const rows = await dataSource
                .getRepository(LarkGroupMember)
                .createQueryBuilder('m')
                .innerJoin(LarkUser, 'u', 'u.union_id = m.union_id')
                .select(['m.union_id AS union_id', 'u.name AS name', 'm.is_leave AS is_leave'])
                .where('m.chat_id = :chatId', { chatId })
                .getRawMany<RosterRow>();

            return rows.map((row) => ({
                unionId: row.union_id,
                name: row.name,
                // 这一列有默认值 false，但老数据里存在 null。null 当成"退群了"会让整
                // 群人突然 @ 不动，所以只认显式的 true。
                hasLeft: row.is_leave === true,
            }));
        },
    };
}
