import { describe, expect, it } from 'bun:test';
import {
  readPixivAuth,
  PIXIV_COOKIE_KEY,
  PIXIV_USER_AGENT_KEY,
  PIXIV_SEC_CH_UA_KEY,
} from './auth';

// 假 DynamicConfig：按预置 map 返回值，缺失返回 ''（与真实 SDK 的 get 默认一致）。
function fakeConfig(map: Record<string, string>) {
  return {
    get: async (key: string): Promise<string> => map[key] ?? '',
  } as any;
}

describe('readPixivAuth：从 Dynamic Config 读 pixiv 鉴权字段', () => {
  it('三项都配了：返回完整 pixiv_auth', async () => {
    const auth = await readPixivAuth(
      fakeConfig({
        [PIXIV_COOKIE_KEY]: 'ck',
        [PIXIV_USER_AGENT_KEY]: 'ua',
        [PIXIV_SEC_CH_UA_KEY]: 'sec',
      }),
    );
    expect(auth).toEqual({ cookie: 'ck', user_agent: 'ua', sec_ch_ua: 'sec' });
  });

  it('只配了 cookie：只返回非空字段（空的交给 server 逐字段回退）', async () => {
    const auth = await readPixivAuth(fakeConfig({ [PIXIV_COOKIE_KEY]: 'ck' }));
    expect(auth).toEqual({ cookie: 'ck' });
  });

  it('三项都没配：返回 undefined（worker 不带 pixiv_auth，server 全量回退 Redis）', async () => {
    const auth = await readPixivAuth(fakeConfig({}));
    expect(auth).toBeUndefined();
  });
});
