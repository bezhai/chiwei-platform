// 人设展示名：群里被 @ 的时候读到的应该是"@赤尾"，不是飞书后台那个应用名。
//
// `bot_persona` 不是飞书的表 —— agent-service 也读它，它属于人设层。所以本服务
// **不注册它的实体**（src/entities 那份清单说的是飞书独占的七张表，多塞一张就不再
// 是那个意思了），只读两列。
//
// 启动时一次性读完、之后按 persona_id 同步查：解析是同步的，不能每碰到一个 @ 就
// 去问一次库。返回的是一个闭包而不是模块级缓存 —— 缓存归组装根持有，谁在用一眼
// 可见，测试之间也不会互相串。

import type { LarkPersonaName } from './bot-lookup';

/** 只需要能跑一句只读查询。生产上是 TypeORM 的 DataSource。 */
export interface PersonaNameSource {
    query(sql: string, params?: unknown[]): Promise<unknown[]>;
}

export async function loadLarkPersonaNames(
    source: PersonaNameSource,
    personaIds: readonly string[],
): Promise<LarkPersonaName> {
    const wanted = [...new Set(personaIds)];
    if (wanted.length === 0) return () => null;

    const rows = (await source.query(
        'SELECT persona_id, display_name FROM bot_persona WHERE persona_id = ANY($1)',
        [wanted],
    )) as Array<{ persona_id: string; display_name: string }>;

    const names = new Map(rows.map((row) => [row.persona_id, row.display_name]));
    return (personaId) => names.get(personaId) ?? null;
}
