import {
    createDefaultRedisConfig,
    getRedisClient,
    RedisClient,
} from '@inner/shared/cache';
import { withRedisCommandTimeout } from './config';

// 使用共享的 Redis 客户端
const redisClient: RedisClient = getRedisClient(
    withRedisCommandTimeout(createDefaultRedisConfig())
);

export default redisClient;
