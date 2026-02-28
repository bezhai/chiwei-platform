import 'reflect-metadata';
import { DataSource } from 'typeorm';
import { ConversationMessage, LarkUser, LarkGroupChatInfo } from '@inner/shared';
import { ModelProvider, ModelMapping, SchemaMigration } from './entities';

export const AppDataSource = new DataSource({
  type: 'postgres',
  host: process.env.POSTGRES_HOST || 'localhost',
  port: 5432,
  username: process.env.POSTGRES_USER || 'postgres',
  password: process.env.POSTGRES_PASSWORD || '',
  database: process.env.POSTGRES_DB || 'postgres',
  synchronize: false,
  logging: ['error'],
  entities: [ConversationMessage, ModelProvider, ModelMapping, LarkUser, LarkGroupChatInfo, SchemaMigration],
});

export { ConversationMessage, LarkUser, LarkGroupChatInfo };
export { ModelProvider, ModelMapping, SchemaMigration };
