import { DataSource } from 'typeorm';
import {
    LarkEmoji,
    LarkUser,
    LarkGroupMember,
    LarkBaseChatInfo,
    LarkGroupChatInfo,
    LarkCardContext,
    UserGroupBinding,
    LarkUserOpenId,
    ResponseFeedback,
    BotConfig,
    UserBlacklist,
    ConversationMessage,
    AgentResponse,
} from './infrastructure/dal/entities';

const AppDataSource = new DataSource({
    type: 'postgres',
    host: process.env.POSTGRES_HOST!,
    port: 5432,
    username: process.env.POSTGRES_USER!,
    password: process.env.POSTGRES_PASSWORD!,
    database: process.env.POSTGRES_DB!,
    synchronize: process.env.SYNCHRONIZE_DB === 'true', // 是否自动同步数据库结构,
    logging: ['error', 'schema', 'warn'], // 是否启用日志
    entities: [
        LarkEmoji,
        LarkUser,
        LarkGroupMember,
        LarkBaseChatInfo,
        LarkGroupChatInfo,
        LarkCardContext,
        UserGroupBinding,
        LarkUserOpenId,
        ResponseFeedback,
        BotConfig,
        UserBlacklist,
        ConversationMessage,
        AgentResponse,
    ],
});

export default AppDataSource;
