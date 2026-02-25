import Router from '@koa/router';

const router = new Router();

router.get('/api/health', (ctx) => {
    ctx.body = {
        status: 'ok',
        service: 'lark-proxy',
        version: process.env.GIT_SHA || 'unknown',
        timestamp: new Date().toISOString(),
    };
});

export default router;
