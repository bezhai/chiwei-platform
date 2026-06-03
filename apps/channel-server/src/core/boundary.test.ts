import { describe, it, expect } from 'bun:test';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative } from 'node:path';

// ──────────────────────────────────────────────────────────────────────
// core 平台无关边界守卫(替代 dependency-cruiser:零新依赖、随 bun test 进 CI、
// 与 --compile 单二进制零冲突)。
//
// 规则:src/core/** 下任何文件都不许 import 平台 SDK(飞书 @lark/* / @lark-client
// / feishu-card / @larksuiteoapi)、飞书原生类型/实体或任何 plugins/**。这是
// 「平台无关核心 + 平台插件」架构的命门——#228 烂掉就是因为完全没有这道检查,
// 飞书代码慢慢渗回核心。
//
// BASELINE:当前(改造前)已存在的违规文件。它们在 B 阶段会被搬进 plugins/lark
// 或改成走能力端口;在那之前先记进 baseline 容忍,但:
//   - 新增违规文件(不在 baseline)→ 立即 fail(挡住继续往核心塞飞书)
//   - baseline 文件清干净后 → 必须从 baseline 移除(本测试会强制,见下),
//     保证 baseline 单调收缩、最终归零。
// ──────────────────────────────────────────────────────────────────────

const FORBIDDEN = [
    /from\s+['"]@lark\//,
    /from\s+['"]@lark-client/,
    /from\s+['"]feishu-card/,
    /from\s+['"]@larksuiteoapi/,
    /from\s+['"]types\/lark['"]/,
    /from\s+['"]types\/mongo['"]/,
    /from\s+['"](?:@entities\/lark-|[^'"]*\/entities\/lark-)/,
    /import\s+\{[^}]*Lark[^}]*\}\s+from\s+['"](?:@infrastructure\/dal\/entities|infrastructure\/dal\/entities)['"]/,
    /from\s+['"][^'"]*\/plugins\//,
    /from\s+['"]@plugins\//,
];

// 改造前已知违规(相对 src/core 的路径,与本测试同逻辑实测得出)。
// B 阶段把这些搬进 plugins/lark 或改走能力端口后,逐个从这里删,直至归零。
//
// B2 已清空的：engine.ts（utility-redirect 改走中性 responder 注入点，飞书发法
// 进 plugins/lark）、callback/* + media/meme + media/photo/*（飞书专属服务整体
// 搬进 plugins/lark/services）。它们都已从 core 移除或脱离飞书 SDK。
//
// B3 已清空：message-builder.ts 迁入 plugins/lark/message-factory.ts，core 下
// 不再允许任何平台 SDK / plugins import baseline。
const BASELINE = new Set<string>();

const CORE_DIR = join(import.meta.dir);

function walk(dir: string): string[] {
    const out: string[] = [];
    for (const name of readdirSync(dir)) {
        const full = join(dir, name);
        if (statSync(full).isDirectory()) {
            out.push(...walk(full));
        } else if (name.endsWith('.ts') && !name.endsWith('.test.ts')) {
            out.push(full);
        }
    }
    return out;
}

function importsPlatform(file: string): boolean {
    const src = readFileSync(file, 'utf8');
    return FORBIDDEN.some((re) => re.test(src));
}

describe('core 平台无关边界', () => {
    const files = walk(CORE_DIR);
    const violators = files
        .filter(importsPlatform)
        .map((f) => relative(CORE_DIR, f));

    it('没有 baseline 之外的新违规(不许继续往 core 塞平台 SDK)', () => {
        const unexpected = violators.filter((f) => !BASELINE.has(f));
        expect(unexpected).toEqual([]);
    });

    it('baseline 单调收缩:已清干净的文件必须从 baseline 移除', () => {
        // baseline 里列了但实际已不再违规的文件 = 陈旧条目,必须删掉,
        // 否则 baseline 会掩盖"其实已经干净"的真相、永远归不了零。
        const stale = [...BASELINE].filter((f) => !violators.includes(f));
        expect(stale).toEqual([]);
    });
});
