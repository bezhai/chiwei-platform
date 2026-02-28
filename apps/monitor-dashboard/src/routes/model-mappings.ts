import Router from '@koa/router';
import { AppDataSource, ModelMapping } from '../db';

const router = new Router();

router.get('/api/model-mappings', async (ctx) => {
  const repo = AppDataSource.getRepository(ModelMapping);
  const mappings = await repo.find({ order: { created_at: 'DESC' } });
  ctx.body = mappings;
});

router.get('/api/model-mappings/:id', async (ctx) => {
  const repo = AppDataSource.getRepository(ModelMapping);
  const mapping = await repo.findOne({ where: { id: ctx.params.id } });
  if (!mapping) {
    ctx.status = 404;
    ctx.body = { message: 'Not found' };
    return;
  }
  ctx.body = mapping;
});

router.post('/api/model-mappings', async (ctx) => {
  const { alias, provider_name, real_model_name, description, model_config } =
    ctx.request.body as {
      alias?: string;
      provider_name?: string;
      real_model_name?: string;
      description?: string;
      model_config?: Record<string, unknown> | null;
    };

  if (!alias || !provider_name || !real_model_name) {
    ctx.status = 400;
    ctx.body = { message: 'alias, provider_name, real_model_name are required' };
    return;
  }

  const repo = AppDataSource.getRepository(ModelMapping);
  const existing = await repo.findOne({ where: { alias } });
  if (existing) {
    ctx.status = 400;
    ctx.body = { message: 'alias already exists' };
    return;
  }

  const mapping = repo.create({
    alias,
    provider_name,
    real_model_name,
    description: description ?? null,
    model_config: model_config ?? null,
  });

  await repo.save(mapping);
  ctx.body = mapping;
});

router.put('/api/model-mappings/:id', async (ctx) => {
  const { alias, provider_name, real_model_name, description, model_config } =
    ctx.request.body as {
      alias?: string;
      provider_name?: string;
      real_model_name?: string;
      description?: string;
      model_config?: Record<string, unknown> | null;
    };

  const repo = AppDataSource.getRepository(ModelMapping);
  const mapping = await repo.findOne({ where: { id: ctx.params.id } });
  if (!mapping) {
    ctx.status = 404;
    ctx.body = { message: 'Not found' };
    return;
  }

  if (alias !== undefined && alias !== mapping.alias) {
    const existing = await repo.findOne({ where: { alias } });
    if (existing) {
      ctx.status = 400;
      ctx.body = { message: 'alias already exists' };
      return;
    }
    mapping.alias = alias;
  }
  if (provider_name !== undefined) {
    mapping.provider_name = provider_name;
  }
  if (real_model_name !== undefined) {
    mapping.real_model_name = real_model_name;
  }
  if (description !== undefined) {
    mapping.description = description;
  }
  if (model_config !== undefined) {
    mapping.model_config = model_config;
  }

  await repo.save(mapping);
  ctx.body = mapping;
});

router.delete('/api/model-mappings/:id', async (ctx) => {
  const repo = AppDataSource.getRepository(ModelMapping);
  const mapping = await repo.findOne({ where: { id: ctx.params.id } });
  if (!mapping) {
    ctx.status = 404;
    ctx.body = { message: 'Not found' };
    return;
  }

  await repo.remove(mapping);
  ctx.body = { success: true };
});

export default router;
