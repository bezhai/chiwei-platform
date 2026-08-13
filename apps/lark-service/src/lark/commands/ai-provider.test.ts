// 302.ai 那两个管理端点的适配器。
//
// 只有一个用户（「余额」指令），而它把结果贴进一张飞书卡片给管理员看。所以这里要钉的
// 不是"能不能解析 JSON"，是三件会静默给出**错误数字**或**静默不给数字**的事：
//
//   * 打的是哪个地址；
//   * 密钥有没有真的带上（少了它 302.ai 返回 401，而 401 在这一层必须是"抛"）；
//   * 非 2xx 一定要抛 —— fetch 对 4xx/5xx 不抛，只把状态码放在 ok 上。

import { describe, expect, it } from 'bun:test';

import { AI_PROVIDER_BASE_URL, httpAiProviderAccount } from './ai-provider';

interface Call {
    url: string;
    headers: Record<string, string>;
}

function stub(
    responses: Record<string, { status: number; body: unknown }>,
): { calls: Call[]; fetchImpl: typeof fetch } {
    const calls: Call[] = [];
    const fetchImpl = (async (url: string, init?: RequestInit) => {
        calls.push({ url, headers: (init?.headers ?? {}) as Record<string, string> });
        const answer = responses[url];
        if (!answer) throw new Error(`unexpected request to ${url}`);
        return {
            ok: answer.status >= 200 && answer.status < 300,
            status: answer.status,
            statusText: '',
            json: async () => answer.body,
        };
    }) as unknown as typeof fetch;
    return { calls, fetchImpl };
}

const BALANCE_URL = `${AI_PROVIDER_BASE_URL}/dashboard/balance`;
const KEYS_URL = `${AI_PROVIDER_BASE_URL}/dashboard/api_keys`;

describe('302.ai 账户查询', () => {
    it('余额从 data.balance 那一层取出来，密钥带在 Authorization 上', async () => {
        const { calls, fetchImpl } = stub({
            [BALANCE_URL]: { status: 200, body: { data: { balance: '123.45' } } },
        });
        const account = httpAiProviderAccount('sk-secret', fetchImpl);

        expect(await account.balance()).toBe('123.45');
        expect(calls).toEqual([
            {
                url: BALANCE_URL,
                headers: {
                    'Content-Type': 'application/json',
                    Authorization: 'Bearer sk-secret',
                },
            },
        ]);
    });

    it('每个 key 的用量从 data 那一层取出来', async () => {
        const { calls, fetchImpl } = stub({
            [KEYS_URL]: {
                status: 200,
                body: {
                    code: 0,
                    message: 'ok',
                    data: [
                        {
                            api_name: 'gemini',
                            limit_daily_cost: 1000,
                            current_date_cost: 250,
                            limit_cost: 0,
                            current_cost: 8000,
                        },
                    ],
                },
            },
        });
        const account = httpAiProviderAccount('sk-secret', fetchImpl);

        expect(await account.apiKeys()).toEqual([
            {
                api_name: 'gemini',
                limit_daily_cost: 1000,
                current_date_cost: 250,
                limit_cost: 0,
                current_cost: 8000,
            },
        ]);
        expect(calls[0]!.url).toBe(KEYS_URL);
    });

    // fetch 对 4xx/5xx 不抛。不看 ok 的话，一段 HTML 错误页会被拿去 JSON.parse，
    // 运气不好还能解析成功 —— 卡片上就出现一个空余额，而没有任何错误。
    it('非 2xx 抛，不静默给出一个空数字', async () => {
        const { fetchImpl } = stub({
            [BALANCE_URL]: { status: 401, body: {} },
            [KEYS_URL]: { status: 500, body: {} },
        });
        const account = httpAiProviderAccount('sk-wrong', fetchImpl);

        expect(account.balance()).rejects.toThrow(/401/);
        expect(account.apiKeys()).rejects.toThrow(/500/);
    });

    // 密钥来自 env，缺配置时上游发出去的是 `Bearer undefined` 并拿一个 401。这里在
    // 装配期就说清楚缺谁 —— config.ts 已经把 AI_PROVIDER_ADMIN_KEY 列进启动检查，
    // 这条是它的第二道门。
    it('没有密钥就在装配期抛', () => {
        expect(() => httpAiProviderAccount(undefined)).toThrow(/AI_PROVIDER_ADMIN_KEY/);
    });
});
