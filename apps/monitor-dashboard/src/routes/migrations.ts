import Router from '@koa/router';
import { AppDataSource, SchemaMigration } from '../db';

const router = new Router();

router.get('/api/migrations', async (ctx) => {
  const repo = AppDataSource.getRepository(SchemaMigration);
  const migrations = await repo.find({ order: { id: 'DESC' } });
  ctx.body = migrations;
});

router.post('/api/migrations/run', async (ctx) => {
  const { version, name, sql, applied_by } = ctx.request.body as {
    version?: string;
    name?: string;
    sql?: string;
    applied_by?: string;
  };

  if (!version || !name || !sql) {
    ctx.status = 400;
    ctx.body = { message: 'version, name, sql are required' };
    return;
  }

  const repo = AppDataSource.getRepository(SchemaMigration);

  const existing = await repo.findOne({ where: { version } });
  if (existing) {
    ctx.status = 409;
    ctx.body = { message: `Migration ${version} already applied` };
    return;
  }

  const startTime = Date.now();
  let status: 'success' | 'failed' = 'success';
  let errorMessage: string | null = null;

  try {
    await AppDataSource.transaction(async (manager) => {
      await manager.query(sql);
    });
  } catch (err) {
    status = 'failed';
    errorMessage = err instanceof Error ? err.message : String(err);
  }

  const durationMs = Date.now() - startTime;

  const record = repo.create({
    version,
    name,
    sql_content: sql,
    applied_by: applied_by || 'manual',
    status,
    error_message: errorMessage,
    duration_ms: durationMs,
  });

  await repo.save(record);

  if (status === 'failed') {
    ctx.status = 500;
    ctx.body = { message: errorMessage, record };
    return;
  }

  ctx.body = record;
});

export default router;
