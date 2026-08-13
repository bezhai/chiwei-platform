import { describe, expect, it } from 'bun:test';
import { getMetadataArgsStorage } from 'typeorm';
import { LarkBaseChatInfo, LarkMessage, LarkUserOpenId } from './index';

// 本服务独占的表如何挂到公共层。公共层自身的表名/列名契约在
// packages/ts-shared/src/entities/common-layer.metadata.test.ts。

function tableName(target: Function): string | undefined {
    return getMetadataArgsStorage().tables.find((t) => t.target === target)?.name;
}

function columnNames(target: Function): string[] {
    return getMetadataArgsStorage()
        .columns.filter((c) => c.target === target)
        .map((c) => (c.options.name as string | undefined) ?? c.propertyName);
}

describe('service-owned entity metadata', () => {
    it('keeps lark native mapping in lark-owned tables', () => {
        expect(tableName(LarkMessage)).toBe('lark_message');
        expect(columnNames(LarkMessage)).toContain('common_message_id');
        expect(columnNames(LarkUserOpenId)).toContain('common_user_id');
        expect(columnNames(LarkBaseChatInfo)).toContain('common_conversation_id');
    });
});
