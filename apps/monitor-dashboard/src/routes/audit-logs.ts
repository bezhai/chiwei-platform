import { Hono } from 'hono';
import { AppDataSource } from '../db';
import { AuditLog } from '../entities/audit-log';

const app = new Hono();

/** GET /api/audit-logs — 查询审计日志，支持过滤和分页 */
app.get('/api/audit-logs', async (c) => {
  const caller = c.req.query('caller');
  const action = c.req.query('action');
  const result = c.req.query('result');
  const from = c.req.query('from');
  const to = c.req.query('to');
  const page = c.req.query('page') || '1';
  const pageSize = c.req.query('pageSize') || '50';

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

  return c.json({
    items,
    total,
    page: Number(page) || 1,
    pageSize: limit,
  });
});

export default app;
