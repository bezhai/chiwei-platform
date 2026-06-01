import 'reflect-metadata';
import { DataSource } from 'typeorm';
import {
  CommonUser,
  CommonConversation,
  CommonMessage,
  CommonAgentResponse,
} from '@inner/shared';
import { ModelProvider, ModelMapping, SchemaMigration, AuditLog, DiaryEntry, WeeklyReview } from './entities';

export const AppDataSource = new DataSource({
  type: 'postgres',
  host: process.env.POSTGRES_HOST || 'localhost',
  port: 5432,
  username: process.env.POSTGRES_USER || 'postgres',
  password: process.env.POSTGRES_PASSWORD || '',
  database: process.env.POSTGRES_DB || 'postgres',
  synchronize: false,
  logging: ['error'],
  entities: [
    CommonUser,
    CommonConversation,
    CommonMessage,
    CommonAgentResponse,
    ModelProvider,
    ModelMapping,
    SchemaMigration,
    AuditLog,
    DiaryEntry,
    WeeklyReview,
  ],
});

export {
  CommonUser,
  CommonConversation,
  CommonMessage,
  CommonAgentResponse,
};
export { ModelProvider, ModelMapping, SchemaMigration, AuditLog, DiaryEntry, WeeklyReview };
