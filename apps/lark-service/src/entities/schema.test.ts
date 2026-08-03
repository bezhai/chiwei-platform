import { beforeAll, describe, expect, it } from 'bun:test';
import { DataSource, type EntityMetadata } from 'typeorm';

import { LARK_ENTITIES } from './index';

// lark_* 七张表是**已经存在的物理表**，本服务只是换了个代码所有者。字段对不上
// 的症状不是编译错误，是运行期 insert 报 column does not exist —— 而 cutover 窗口
// 里 channel-server 还在写同样的表，一处写错就是两个服务对同一张表的理解分叉。
//
// 下面这份 EXPECTED 不是照着新实体抄的，是从**现有实现**解析出来的：用
// apps/channel-server/src/infrastructure/dal/entities 的七个类喂给一次性 DataSource
// 跑 buildMetadatas()，把每列的 databaseName / type / length / nullable / array /
// default 与索引、关系原样导出，再逐条写进这里。因此这份期望描述的是"今天生产上
// 跑的映射"，本服务的实体必须长成它，而不是反过来。
//
// 交叉核对过 scripts/db/001-common-layer-schema.sql：lark_message 的建表语句、
// lark_user_open_id.common_user_id、lark_base_chat_info.common_conversation_id 与
// 两条索引名一致。已知偏差一处并**刻意保留**：SQL 里 lark_message.created_at 是
// timestamptz，实体声明的是 timestamp —— synchronize 关着，实体的类型只影响
// TypeORM 自己的取值转换，改它属于行为变更，不属于本次拆分。

interface ExpectedColumn {
    property: string;
    column: string;
    type: string;
    length: string;
    nullable: boolean;
    array: boolean;
    default: string | boolean | null;
    createDate: boolean;
    updateDate: boolean;
}

interface ExpectedTable {
    entity: string;
    table: string;
    primary: string[];
    columns: ExpectedColumn[];
    indices: Array<{ name: string; columns: string[]; unique: boolean }>;
    relations: Array<{ property: string; type: string; joinColumns: string[] }>;
}

function column(
    property: string,
    column: string,
    type: string,
    options: Partial<Omit<ExpectedColumn, 'property' | 'column' | 'type'>> = {},
): ExpectedColumn {
    return {
        property,
        column,
        type,
        length: options.length ?? '',
        nullable: options.nullable ?? false,
        array: options.array ?? false,
        default: options.default ?? null,
        createDate: options.createDate ?? false,
        updateDate: options.updateDate ?? false,
    };
}

const EXPECTED: ExpectedTable[] = [
    {
        entity: 'LarkBaseChatInfo',
        table: 'lark_base_chat_info',
        primary: ['chat_id'],
        columns: [
            column('chat_id', 'chat_id', 'String'),
            column('chat_mode', 'chat_mode', 'varchar', { length: '10' }),
            column('common_conversation_id', 'common_conversation_id', 'uuid', {
                nullable: true,
            }),
            column('gray_config', 'gray_config', 'jsonb', { nullable: true }),
            column('permission_config', 'permission_config', 'jsonb', { nullable: true }),
        ],
        indices: [
            {
                name: 'uq_lark_base_chat_info_common_conversation_id',
                columns: ['common_conversation_id'],
                unique: true,
            },
        ],
        relations: [],
    },
    {
        entity: 'LarkEmoji',
        table: 'lark_emoji',
        primary: ['key'],
        columns: [
            column('createdAt', 'created_at', 'timestamp', {
                default: 'sql:now()',
                createDate: true,
            }),
            column('key', 'key', 'varchar', { length: '100' }),
            column('text', 'text', 'varchar', { length: '500' }),
            column('updatedAt', 'updated_at', 'timestamp', {
                default: 'sql:now()',
                updateDate: true,
            }),
        ],
        indices: [],
        relations: [],
    },
    {
        entity: 'LarkGroupChatInfo',
        table: 'lark_group_chat_info',
        primary: ['chat_id'],
        columns: [
            column('avatar', 'avatar', 'text', { nullable: true }),
            column('chat_id', 'chat_id', 'String'),
            column('chat_status', 'chat_status', 'varchar', { length: '20' }),
            column('chat_tag', 'chat_tag', 'varchar', { length: '255', nullable: true }),
            column('description', 'description', 'text', { nullable: true }),
            column(
                'download_has_permission_setting',
                'download_has_permission_setting',
                'varchar',
                { length: '20', nullable: true },
            ),
            column('group_message_type', 'group_message_type', 'varchar', {
                length: '10',
                nullable: true,
            }),
            column('is_leave', 'is_leave', 'boolean', { default: false }),
            column('name', 'name', 'String'),
            column('user_count', 'user_count', 'int'),
            column('user_manager_id_list', 'user_manager_id_list', 'text', {
                nullable: true,
                array: true,
            }),
        ],
        indices: [],
        // 群聊详情与基础会话信息共用同一列 chat_id：关系是"同主键 1:1"，不是外键列。
        relations: [{ property: 'baseChatInfo', type: 'one-to-one', joinColumns: ['chat_id'] }],
    },
    {
        entity: 'LarkGroupMember',
        table: 'lark_group_member',
        primary: ['chat_id', 'union_id'],
        columns: [
            column('chat_id', 'chat_id', 'String'),
            column('created_at', 'created_at', 'timestamp', {
                default: 'sql:CURRENT_TIMESTAMP',
            }),
            column('is_leave', 'is_leave', 'boolean', { default: false }),
            column('is_manager', 'is_manager', 'boolean', { default: false }),
            column('is_owner', 'is_owner', 'boolean', { default: false }),
            column('union_id', 'union_id', 'String'),
            column('updated_at', 'updated_at', 'timestamp', {
                default: 'sql:CURRENT_TIMESTAMP',
            }),
        ],
        indices: [],
        relations: [],
    },
    {
        entity: 'LarkMessage',
        table: 'lark_message',
        primary: ['om_id'],
        columns: [
            column('chat_id', 'chat_id', 'varchar', { length: '256' }),
            column('common_message_id', 'common_message_id', 'uuid'),
            column('created_at', 'created_at', 'timestamp', {
                default: 'sql:now()',
                createDate: true,
            }),
            column('message_type', 'message_type', 'varchar', { length: '64' }),
            column('om_id', 'om_id', 'varchar', { length: '256' }),
            column('raw_event', 'raw_event', 'jsonb', { nullable: true }),
            column('reply_om_id', 'reply_om_id', 'varchar', { length: '256', nullable: true }),
            column('root_om_id', 'root_om_id', 'varchar', { length: '256', nullable: true }),
            column('sender_open_id', 'sender_open_id', 'varchar', {
                length: '256',
                nullable: true,
            }),
            column('sender_union_id', 'sender_union_id', 'varchar', {
                length: '256',
                nullable: true,
            }),
        ],
        indices: [
            { name: 'idx_lark_message_chat_id', columns: ['chat_id'], unique: false },
            {
                name: 'uq_lark_message_common_message_id',
                columns: ['common_message_id'],
                unique: true,
            },
        ],
        relations: [],
    },
    {
        entity: 'LarkUser',
        table: 'lark_user',
        primary: ['union_id'],
        columns: [
            column('avatar_origin', 'avatar_origin', 'text', { nullable: true }),
            column('is_admin', 'is_admin', 'boolean', { nullable: true }),
            column('name', 'name', 'String'),
            column('union_id', 'union_id', 'String'),
        ],
        indices: [],
        relations: [],
    },
    {
        entity: 'LarkUserOpenId',
        table: 'lark_user_open_id',
        primary: ['app_id', 'open_id'],
        columns: [
            column('appId', 'app_id', 'varchar'),
            column('commonUserId', 'common_user_id', 'uuid', { nullable: true }),
            column('name', 'name', 'varchar'),
            column('openId', 'open_id', 'varchar'),
            column('unionId', 'union_id', 'varchar', { nullable: true }),
        ],
        indices: [
            {
                name: 'idx_lark_user_open_id_common_user_id',
                columns: ['common_user_id'],
                unique: false,
            },
        ],
        relations: [],
    },
];

type MetadataBuildable = DataSource & { buildMetadatas(): Promise<void> };

function describeTable(meta: EntityMetadata): ExpectedTable {
    return {
        entity: meta.name,
        table: meta.tableName,
        primary: meta.primaryColumns.map((c) => c.databaseName).sort(),
        columns: meta.columns
            .map((c) => ({
                property: c.propertyName,
                column: c.databaseName,
                type: typeof c.type === 'function' ? c.type.name : String(c.type),
                length: c.length,
                nullable: c.isNullable,
                array: c.isArray,
                default:
                    c.default === undefined
                        ? null
                        : typeof c.default === 'function'
                          ? `sql:${c.default()}`
                          : (c.default as string | boolean),
                createDate: c.isCreateDate,
                updateDate: c.isUpdateDate,
            }))
            .sort((a, b) => a.column.localeCompare(b.column)),
        indices: meta.indices
            .map((i) => ({
                name: i.name,
                columns: i.columns.map((c) => c.databaseName),
                unique: i.isUnique,
            }))
            .sort((a, b) => a.name.localeCompare(b.name)),
        relations: meta.relations
            .map((r) => ({
                property: r.propertyName,
                type: String(r.relationType),
                joinColumns: r.joinColumns.map((c) => c.databaseName),
            }))
            .sort((a, b) => a.property.localeCompare(b.property)),
    };
}

let probe: DataSource;

beforeAll(async () => {
    // 只解析 metadata，不建连接：initialize() 在连库之前做的正是这一步。
    probe = new DataSource({
        type: 'postgres',
        host: 'unused',
        port: 5432,
        username: 'unused',
        password: 'unused',
        database: 'unused',
        synchronize: false,
        entities: [...LARK_ENTITIES],
    });
    await (probe as MetadataBuildable).buildMetadatas();
});

describe('lark_* entity mapping matches the physical schema', () => {
    it('owns exactly the seven lark tables', () => {
        expect(probe.entityMetadatas.map((m) => m.tableName).sort()).toEqual(
            EXPECTED.map((t) => t.table).sort(),
        );
        expect(LARK_ENTITIES).toHaveLength(EXPECTED.length);
    });

    it.each(EXPECTED.map((t) => [t.table, t] as const))('maps %s field by field', (_table, spec) => {
        const meta = probe.entityMetadatas.find((m) => m.tableName === spec.table);
        expect(meta).toBeDefined();
        expect(describeTable(meta!)).toEqual(spec);
    });

    // onUpdate 不在通用比对里（它只出现在这一列上），单独钉死：这列靠 DB 侧
    // CURRENT_TIMESTAMP 更新，去掉之后成员表的 updated_at 会永远停在插入时间。
    it('keeps the DB-side updated_at trigger on lark_group_member', () => {
        const meta = probe.entityMetadatas.find((m) => m.tableName === 'lark_group_member');
        expect(meta!.findColumnWithDatabaseName('updated_at')!.onUpdate).toBe('CURRENT_TIMESTAMP');
    });
});
