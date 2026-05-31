import { describe, it, expect } from 'bun:test';
import { shouldEnableDirectIngress } from './ingress-gate';

// 飞书直连入口（webhook + ws）的 env gate 纯逻辑。默认 off 是双跑红线：
// channel-proxy 在 ③ cutover 前仍是飞书入口，channel-server 绝不能同时连同一 bot。
// 只有 LARK_DIRECT_INGRESS 显式 'true' 才开。
describe('shouldEnableDirectIngress', () => {
    it('未设置 → off（默认零回归，proxy 仍是入口）', () => {
        expect(shouldEnableDirectIngress(undefined)).toBe(false);
    });
    it("'false' / 其它值 → off", () => {
        expect(shouldEnableDirectIngress('false')).toBe(false);
        expect(shouldEnableDirectIngress('1')).toBe(false);
        expect(shouldEnableDirectIngress('')).toBe(false);
    });
    it("'true' → on（③ cutover 在场才开）", () => {
        expect(shouldEnableDirectIngress('true')).toBe(true);
    });
});
