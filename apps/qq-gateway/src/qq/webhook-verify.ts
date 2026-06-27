/**
 * QQ webhook 签名 — Ed25519。
 *
 * QQ 开放平台用 Ed25519 做回调验签：
 *   1. bot secret 重复拼接到 >= 32 字节后截断成 32 字节 seed。
 *   2. 用公钥校验 `timestamp + body` 是否匹配 `X-Signature-Ed25519`。
 *   3. 回调地址校验（op:13）时，对 `event_ts + plain_token` 签名。
 *
 * 移植自 openclaw-qqbot/src/transport/webhook-verify.ts，仅依赖 node:crypto，
 * 不引入任何 openclaw 框架代码。
 */

import * as crypto from 'node:crypto';

/** 由 bot secret 推导 32 字节 Ed25519 seed：重复拼接到 >=32 后截断。 */
function deriveSeed(botSecret: string): Buffer {
    let seed = botSecret;
    while (seed.length < 32) {
        seed = seed + seed;
    }
    return Buffer.from(seed.slice(0, 32), 'utf-8');
}

/** 由 bot secret 生成 Ed25519 公私钥对。 */
function getKeyPair(botSecret: string): { privateKey: crypto.KeyObject; publicKey: crypto.KeyObject } {
    const seed = deriveSeed(botSecret);
    const privateKey = crypto.createPrivateKey({
        key: Buffer.concat([
            // 32 字节 seed 的 Ed25519 PKCS8 DER 前缀
            Buffer.from('302e020100300506032b657004220420', 'hex'),
            seed,
        ]),
        format: 'der',
        type: 'pkcs8',
    });
    const publicKey = crypto.createPublicKey(privateKey);
    return { privateKey, publicKey };
}

/** 用 bot 私钥对消息签名，返回 hex 字符串。 */
export function ed25519Sign(botSecret: string, message: Buffer): string {
    const { privateKey } = getKeyPair(botSecret);
    return crypto.sign(null, message, privateKey).toString('hex');
}

/**
 * 校验 QQ webhook 回调请求的 Ed25519 签名。
 * 签名内容为 `timestamp + body`。校验失败或异常一律返回 false（绝不抛）。
 */
export function verifyWebhookSignature(params: {
    body: Buffer;
    timestamp: string;
    signature: string;
    botSecret: string;
}): boolean {
    const { body, timestamp, signature, botSecret } = params;
    try {
        const { publicKey } = getKeyPair(botSecret);
        const message = Buffer.concat([Buffer.from(timestamp, 'utf-8'), body]);
        const sigBuffer = Buffer.from(signature, 'hex');
        // 非法 hex 会被静默截断成短 buffer；显式拒绝长度异常的签名
        if (sigBuffer.length !== 64) return false;
        return crypto.verify(null, message, publicKey, sigBuffer);
    } catch {
        return false;
    }
}

/**
 * 生成回调地址校验（op:13）的响应。
 * QQ 发来 `{ op: 13, d: { plain_token, event_ts } }`，须回 `{ plain_token, signature }`，
 * 其中 signature 是对 `event_ts + plain_token` 的 Ed25519 签名。
 */
export function signValidationResponse(params: {
    plainToken: string;
    eventTs: string;
    botSecret: string;
}): { plain_token: string; signature: string } {
    const { plainToken, eventTs, botSecret } = params;
    const message = Buffer.from(eventTs + plainToken, 'utf-8');
    const signature = ed25519Sign(botSecret, message);
    return { plain_token: plainToken, signature };
}
