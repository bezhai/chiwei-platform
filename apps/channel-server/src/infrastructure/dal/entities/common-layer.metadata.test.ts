import { describe, expect, it } from 'bun:test';
import { getMetadataArgsStorage } from 'typeorm';
import {
    CommonAgentResponse,
    CommonBotPresence,
    CommonConversation,
    CommonMessage,
    CommonUser,
    BotConfig,
    LarkBaseChatInfo,
    LarkMessage,
    LarkUserOpenId,
} from './index';

function tableName(target: Function): string | undefined {
    return getMetadataArgsStorage().tables.find((t) => t.target === target)?.name;
}

function columnNames(target: Function): string[] {
    return getMetadataArgsStorage()
        .columns.filter((c) => c.target === target)
        .map((c) => (c.options.name as string | undefined) ?? c.propertyName);
}

describe('common/lark layer entity metadata', () => {
    it('registers common layer tables', () => {
        expect(tableName(CommonUser)).toBe('common_user');
        expect(tableName(CommonConversation)).toBe('common_conversation');
        expect(tableName(CommonMessage)).toBe('common_message');
        expect(tableName(CommonAgentResponse)).toBe('common_agent_response');
        expect(tableName(CommonBotPresence)).toBe('common_bot_presence');
    });

    it('bot_config carries bot common user identity', () => {
        expect(columnNames(BotConfig)).toContain('common_user_id');
    });

    it('keeps lark native mapping in lark-owned tables', () => {
        expect(tableName(LarkMessage)).toBe('lark_message');
        expect(columnNames(LarkMessage)).toContain('common_message_id');
        expect(columnNames(LarkUserOpenId)).toContain('common_user_id');
        expect(columnNames(LarkBaseChatInfo)).toContain('common_conversation_id');
    });

    it('does not put lark raw ids on common message', () => {
        const commonMessageColumns = columnNames(CommonMessage);

        expect(commonMessageColumns).toContain('common_message_id');
        expect(commonMessageColumns).toContain('common_conversation_id');
        expect(commonMessageColumns).toContain('common_user_id');
        expect(commonMessageColumns).not.toContain('om_id');
        expect(commonMessageColumns).not.toContain('chat_id');
        expect(commonMessageColumns).not.toContain('open_id');
    });
});
