import type { EntityManager } from 'typeorm';

/** 入站和出站共用同一个事务锁；数据库提交或回滚时自动释放。 */
export async function lockLarkMessage(manager: EntityManager, omId: string): Promise<void> {
    if (!manager.queryRunner?.isTransactionActive) {
        throw new Error('lark message lock requires an active transaction');
    }
    await manager.query('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))', [
        `lark-message:${omId}`,
    ]);
}
