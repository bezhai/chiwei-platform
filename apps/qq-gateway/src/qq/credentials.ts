/**
 * QQ 凭据来源：bot_config.credentials (jsonb)，按 bot_name 读，不再走 env。
 *
 * 项目约定 bot 凭据统一存 bot_config.credentials，由各 channel 的 adapter 解释 blob。
 * 本模块只取 qq 形态里的 app_id / app_secret（刷 access_token 所需），bot_secret 暂不使用。
 *
 * 「执行 SQL」抽象成注入的 query 执行器：纯解析逻辑可单测，生产再接真实 DB（见 index.ts）。
 */

export interface QQCredentials {
    appId: string;
    appSecret: string;
}

/** 注入的查询执行器：跑参数化 SQL，返回行数组。 */
export type CredentialsQuery = (text: string, params: unknown[]) => Promise<Array<Record<string, unknown>>>;

export interface LoadQQCredentialsDeps {
    query: CredentialsQuery;
}

const SELECT_QQ_CREDENTIALS = `SELECT credentials FROM bot_config WHERE bot_name = $1 AND channel = 'qq'`;

/**
 * 从 bot_config 读出指定 bot 的 QQ 凭据。
 * 行不存在 / credentials 为 null 或非对象 / 缺 app_id 或 app_secret → fail-fast，错误信息带 botName。
 */
export async function loadQQCredentials(botName: string, deps: LoadQQCredentialsDeps): Promise<QQCredentials> {
    const rows = await deps.query(SELECT_QQ_CREDENTIALS, [botName]);
    const row = rows[0];
    if (!row) {
        throw new Error(`qq-gateway: no bot_config row for bot_name=${botName} (channel=qq)`);
    }

    const credentials = row.credentials;
    if (credentials === null || typeof credentials !== 'object') {
        throw new Error(`qq-gateway: bot_config.credentials missing or not an object for bot_name=${botName}`);
    }

    const blob = credentials as Record<string, unknown>;
    const appId = blob.app_id;
    const appSecret = blob.app_secret;
    if (typeof appId !== 'string' || appId === '') {
        throw new Error(`qq-gateway: bot_config.credentials.app_id missing for bot_name=${botName}`);
    }
    if (typeof appSecret !== 'string' || appSecret === '') {
        throw new Error(`qq-gateway: bot_config.credentials.app_secret missing for bot_name=${botName}`);
    }

    return { appId, appSecret };
}
