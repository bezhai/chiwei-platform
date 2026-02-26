import axios, { AxiosResponse } from 'axios';
import crypto from 'crypto';

/**
 * 生成随机盐
 */
export function generateSalt(length: number): string {
    const letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
    const bytes = crypto.randomBytes(length);
    let salt = '';
    for (let i = 0; i < length; i++) {
        salt += letters[bytes[i] % letters.length];
    }
    return salt;
}

/**
 * 生成认证 Token
 */
export function generateToken(salt: string, body: string, secret: string): string {
    const data = salt + body + secret;
    return crypto.createHash('sha256').update(data).digest('hex');
}

/**
 * 发送带认证的 POST 请求
 */
export async function sendAuthenticatedRequest<T>(
    url: string,
    reqBody: Record<string, any>,
    secret: string,
    httpClient?: typeof axios
): Promise<T> {
    const client = httpClient || axios;
    const salt = generateSalt(10);
    const token = generateToken(salt, JSON.stringify(reqBody), secret);

    try {
        const response: AxiosResponse<T> = await client.post<T>(url, reqBody, {
            headers: {
                'X-Salt': salt,
                'X-Token': token,
                'Content-Type': 'application/json',
            },
        });

        return response.data;
    } catch (error: any) {
        console.error('Error in sendAuthenticatedRequest:', error);
        throw new Error(`Request failed: ${error.message}`);
    }
}

/**
 * URL 编码参数
 */
export function urlEncode(params: Record<string, any>): string {
    const encode = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
        if (typeof v === 'string' && v !== '') {
            encode.append(k, v);
        } else if (Array.isArray(v)) {
            for (const val of v) {
                if (val !== '') {
                    encode.append(k, val);
                }
            }
        } else {
            const strVal = String(v ?? '');
            if (strVal !== '') {
                encode.append(k, strVal);
            }
        }
    }
    return encode.toString();
}
