import Router from '@koa/router';
import { AppDataSource } from '../db';
import { AuditLog } from '../entities/audit-log';

const router = new Router();

/** GET /api/audit-logs — 查询审计日志，支持过滤和分页 */
router.get('/api/audit-logs', async (ctx) => {
  const {
    caller,
    action,
    result,
    from,
    to,
    page = '1',
    pageSize = '50',
  } = ctx.query as Record<string, string | undefined>;

  const repo = AppDataSource.getRepository(AuditLog);
  const qb = repo.createQueryBuilder('log');

  if (caller) qb.andWhere('log.caller = :caller', { caller });
  if (action) qb.andWhere('log.action LIKE :action', { action: `%${action}%` });
  if (result) qb.andWhere('log.result = :result', { result });
  if (from) qb.andWhere('log.created_at >= :from', { from });
  if (to) qb.andWhere('log.created_at <= :to', { to });

  const limit = Math.min(Number(pageSize) || 50, 200);
  const offset = ((Number(page) || 1) - 1) * limit;

  qb.orderBy('log.created_at', 'DESC').skip(offset).take(limit);

  const [items, total] = await qb.getManyAndCount();

  ctx.body = {
    items,
    total,
    page: Number(page) || 1,
    pageSize: limit,
  };
});

export default router;
