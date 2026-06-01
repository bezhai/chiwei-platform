import { DataSource } from 'typeorm';
import {
    LarkEmoji,
    LarkUser,
    LarkGroupMember,
    LarkBaseChatInfo,
    LarkGroupChatInfo,
    UserGroupBinding,
    LarkUserOpenId,
    BotConfig,
    BotPersona,
    UserBlacklist,
    BotChatPresence,
    CommonUser,
    CommonConversation,
    CommonMessage,
    CommonAgentResponse,
    LarkMessage,
    LaneRouting,
} from './infrastructure/dal/entities';

const AppDataSource = new DataSource({
    type: 'postgres',
    host: process.env.POSTGRES_HOST!,
    port: Number(process.env.POSTGRES_PORT) || 5432,
    username: process.env.POSTGRES_USER!,
    password: process.env.POSTGRES_PASSWORD!,
    database: process.env.POSTGRES_DB!,
    synchronize: false, // 禁止 ORM 在启动时 sync schema; DDL 走 /ops-db submit 或 migration
    logging: ['error', 'schema', 'warn'], // 是否启用日志
    entities: [
        LarkEmoji,
        LarkUser,
        LarkGroupMember,
        LarkBaseChatInfo,
        LarkGroupChatInfo,
        UserGroupBinding,
        LarkUserOpenId,
        BotConfig,
        BotPersona,
        UserBlacklist,
        BotChatPresence,
        CommonUser,
        CommonConversation,
        CommonMessage,
        CommonAgentResponse,
        LarkMessage,
        LaneRouting,
    ],
});

export default AppDataSource;
