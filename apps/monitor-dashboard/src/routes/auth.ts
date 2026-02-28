import Router from '@koa/router';
import jwt from 'jsonwebtoken';

const router = new Router();

router.post('/api/auth/login', async (ctx) => {
  const { password } = ctx.request.body as { password?: string };

  if (!password || password !== process.env.DASHBOARD_ADMIN_PASSWORD) {
    ctx.status = 401;
    ctx.body = { message: 'Invalid password' };
    return;
  }

  const secret = process.env.DASHBOARD_JWT_SECRET!;
  const token = jwt.sign({ role: 'admin' }, secret, { expiresIn: '24h' });

  ctx.body = { token };
});

export default router;
