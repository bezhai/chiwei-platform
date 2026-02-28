import Router from '@koa/router';
import { AppDataSource, ModelProvider } from '../db';
import { randomUUID } from 'crypto';

const router = new Router();

const maskApiKey = (apiKey: string) => {
  if (!apiKey) {
    return '';
  }
  const tail = apiKey.slice(-4);
  return `****${tail}`;
};

const allowedClientTypes = new Set(['openai', 'ark', 'azure-http', 'google']);

router.get('/api/providers', async (ctx) => {
  const repo = AppDataSource.getRepository(ModelProvider);
  const providers = await repo.find({ order: { created_at: 'DESC' } });
  ctx.body = providers.map((provider) => ({
    ...provider,
    api_key: maskApiKey(provider.api_key),
  }));
});

router.get('/api/providers/:id', async (ctx) => {
  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = await repo.findOne({ where: { provider_id: ctx.params.id } });
  if (!provider) {
    ctx.status = 404;
    ctx.body = { message: 'Not found' };
    return;
  }
  ctx.body = {
    ...provider,
    api_key: maskApiKey(provider.api_key),
  };
});

router.post('/api/providers', async (ctx) => {
  const { name, api_key, base_url, client_type, is_active } = ctx.request.body as {
    name?: string;
    api_key?: string;
    base_url?: string;
    client_type?: string;
    is_active?: boolean;
  };

  if (!name || !base_url || !api_key) {
    ctx.status = 400;
    ctx.body = { message: 'name, base_url, api_key are required' };
    return;
  }

  const resolvedClientType = (client_type || 'openai').toLowerCase();
  if (!allowedClientTypes.has(resolvedClientType)) {
    ctx.status = 400;
    ctx.body = { message: 'Invalid client_type' };
    return;
  }

  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = repo.create({
    provider_id: randomUUID(),
    name,
    api_key,
    base_url,
    client_type: resolvedClientType,
    is_active: is_active ?? true,
  });

  await repo.save(provider);

  ctx.body = {
    ...provider,
    api_key: maskApiKey(provider.api_key),
  };
});

router.put('/api/providers/:id', async (ctx) => {
  const { name, api_key, base_url, client_type, is_active } = ctx.request.body as {
    name?: string;
    api_key?: string;
    base_url?: string;
    client_type?: string;
    is_active?: boolean;
  };

  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = await repo.findOne({ where: { provider_id: ctx.params.id } });
  if (!provider) {
    ctx.status = 404;
    ctx.body = { message: 'Not found' };
    return;
  }

  if (name !== undefined) {
    provider.name = name;
  }
  if (api_key !== undefined && api_key !== '') {
    provider.api_key = api_key;
  }
  if (base_url !== undefined) {
    provider.base_url = base_url;
  }
  if (client_type !== undefined) {
    const resolvedClientType = client_type.toLowerCase();
    if (!allowedClientTypes.has(resolvedClientType)) {
      ctx.status = 400;
      ctx.body = { message: 'Invalid client_type' };
      return;
    }
    provider.client_type = resolvedClientType;
  }
  if (is_active !== undefined) {
    provider.is_active = is_active;
  }

  await repo.save(provider);

  ctx.body = {
    ...provider,
    api_key: maskApiKey(provider.api_key),
  };
});

router.delete('/api/providers/:id', async (ctx) => {
  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = await repo.findOne({ where: { provider_id: ctx.params.id } });
  if (!provider) {
    ctx.status = 404;
    ctx.body = { message: 'Not found' };
    return;
  }

  await repo.remove(provider);
  ctx.body = { success: true };
});

export default router;
