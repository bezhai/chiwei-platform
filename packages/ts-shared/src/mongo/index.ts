// MongoDB client and service
export { MongoService, getMongoService, resetMongoService, createMongoService } from './client';

// MongoDB collection wrapper
export { MongoCollection } from './collection';

// Types and utilities
export { createDefaultMongoConfig, buildMongoUrl } from './types';
export type { MongoConfig, IndexDefinition, BulkWriteResult } from './types';
