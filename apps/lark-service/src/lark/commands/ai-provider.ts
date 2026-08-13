// 我们在 302.ai 上那个账户的两个数字：还剩多少钱、每个 key 花了多少。
//
// 唯一的用户是「余额」指令（balance.ts）。写成端口是因为**开发机打不到 302.ai**，而
// 这条指令要钉的恰恰是"卡片上那几个数字对不对"—— 真身在这里，指令那一侧测的是卡片。
//
// ## 打它走裸 fetch，不走 LaneRouter
//
// 与 ../emoji/sync.ts 同一个理由：LaneRouter 是给**本集群内部**的服务用的（按注册表拼
// `{app}-{lane}` 的主机名、注入 x-ctx-lane），拿它去打一个外部域名没有意义。
//
// ## 非 2xx 一律抛
//
// fetch 对 4xx/5xx 不抛，只把状态码放在 `ok` 上。不看它的话，一段 HTML 错误页会被拿去
// JSON.parse —— 运气不好还能解析成功，于是卡片上出现一个空余额、而没有任何错误。上游
// 用的 axios 默认对 4xx/5xx 抛，所以"非 2xx 抛"就是照搬它的行为。

/** 一个 API key 的用量。字段名是 302.ai 的口径，卡片那一列的 name 直接用它们。 */
export interface LarkAiKeyUsage {
    api_name: string;
    /** 以下四项都是**千分之一**单位的整数，卡片上除以 1000 显示。 */
    limit_daily_cost: number;
    current_date_cost: number;
    limit_cost: number;
    current_cost: number;
}

export interface LarkAiProviderAccount {
    /**
     * 账户余额。
     *
     * **是字符串不是数**：302.ai 就是这么返回的，而卡片上原样显示。转成数再格式化会在
     * 精度上引入一个我们并不需要的决定。
     */
    balance(): Promise<string>;
    apiKeys(): Promise<LarkAiKeyUsage[]>;
}

/** 拆分前写死在代码里的那个地址。 */
export const AI_PROVIDER_BASE_URL = 'https://api.302.ai';

/** 上游那个 axios 客户端的超时是 30 秒，照搬。 */
const TIMEOUT_MS = 30_000;

/**
 * @param adminKey `AI_PROVIDER_ADMIN_KEY`。**没有就在装配期抛** —— 上游缺配置时发出去
 *   的是 `Bearer undefined`，拿一个 401，而症状要到管理员敲「余额」时才出现。
 */
export function httpAiProviderAccount(
    adminKey: string | undefined,
    fetchImpl: typeof fetch = fetch,
): LarkAiProviderAccount {
    if (!adminKey) {
        throw new Error('lark-service: AI_PROVIDER_ADMIN_KEY is required by the 余额 command');
    }

    async function get<T>(path: string): Promise<T> {
        const url = `${AI_PROVIDER_BASE_URL}${path}`;
        const response = await fetchImpl(url, {
            headers: {
                'Content-Type': 'application/json',
                Authorization: `Bearer ${adminKey}`,
            },
            signal: AbortSignal.timeout(TIMEOUT_MS),
        });
        if (!response.ok) {
            throw new Error(`302.ai answered ${response.status} ${response.statusText}`.trim());
        }
        return (await response.json()) as T;
    }

    return {
        async balance() {
            const body = await get<{ data: { balance: string } }>('/dashboard/balance');
            return body.data.balance;
        },
        async apiKeys() {
            const body = await get<{ data: LarkAiKeyUsage[] }>('/dashboard/api_keys');
            return body.data;
        },
    };
}
