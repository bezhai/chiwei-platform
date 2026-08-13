// 「同一段内容在这个群里连着出现第几次了」。
//
// ## 拆分把这段读-改-写的并发前提改了
//
// 拆分前是 `GET` → 在进程内存里加一 → `SET`，三步分开（channel-server 的
// commands/repeat-message.ts）。整个规则段那时跑在按 **om_id** 取的投影锁里，所以
// **同一条消息**被同群几个 bot 各处理一遍时是串行的。
//
// 但那把锁按 om_id 分，计数器按 **chat_id** 分 —— 两个粒度对不上。所以拆分前**也只有
// 同一条消息的那几路是串的**：同一个群里两条挨得很近的消息（不同 om_id）本来就在并发
// 地读同一个 `repeat_msg:{chatId}`。这个竞态不是拆分引入的，拆分只是把它放大了一档：
// lark-service 的 om_id 锁只包投影、规则段在锁外（见 ../rules/inbound-rules.ts 的文件
// 头），于是同一条消息的几个 bot 也开始并发。
//
// 交错的两种症状都不是"少数一次"那么轻：
//
//   * 两条流都读到 2、都写 3、都拿到 3 —— 复读**发两遍**（`=== 3` 对两条都成立）。
//   * 两条流都读到 1、都写 2 —— 3 永远不会被观察到，这一串复读就此哑掉。
//
// ## 修法：把读-改-写整个搬到 Redis 那边，一条命令
//
// 三条路走过一遍：
//
//   * **另取一把锁**（按 chatId）。能用，但换来一整套新的失败模式：抢不到锁怎么办
//     （跳过 = 少数一次；等 = 把入站链路挂在一把跟消息内容无关的锁上），锁泄漏怎么办。
//     为了一个计数器引入一把分布式锁，代价大于收益。
//   * **让它自身幂等**（按 common_message_id 去重，一条人类消息只数一次）。这会**改变**
//     可观测行为：群里有两个跑 utility 规则的 bot 时，拆分前一条消息是加两次的（见下），
//     幂等之后变成一次，复读的触发时机跟着变。而这一批的验收口径是"行为与拆分前一致"。
//   * **一条命令做完**。Redis 单线程执行脚本，读和写之间没有别人插得进来的地方，
//     计数序列因此严格连续，`=== 3` 恰好被一条流观察到一次。语义与拆分前逐字相同 ——
//     包括下面那条"N 个 bot 加 N 次"。选它。
//
// ## 「N 个 bot 加 N 次」是既有形态，这里原样保留
//
// 飞书把同一条群消息推给群里的每个 bot，每个 bot 各走一遍规则段。复读那条规则声明了
// `category: 'utility'`，而共享引擎在 bot 是 persona 角色时会跳过 utility 规则，所以
// 实际会数到的是**群里非 persona 的那几个 bot**（现实中通常只有 `tool` 一个）。真有两
// 个的话，一条人类消息把计数加两次，于是复读在第二条重复消息上就触发 —— 拆分前就是
// 这样，不在这一批里改。
//
// ## 键名和值的形状是跨服务契约
//
// 切换窗口里 channel-server 那份复读还活着（Task F 才删），两边读写同一个键。所以键名、
// JSON 的字段名、7 天过期全部一字不改：换了键名就是同一个群两份计数，换了字段名就是
// 对面永远认为"内容变了"。

/** 本模块用到的 Redis 表面，就这一个命令。 */
export interface RepeatCounterRedis {
    evalScript(
        script: string,
        numKeys: number,
        ...keysAndArgs: (string | number)[]
    ): Promise<unknown>;
}

export interface LarkRepeatCounter {
    /**
     * 这段内容在这个会话上连着出现第几次了。内容变了就从 1 重新数。
     *
     * **实现必须是原子的**：调用方拿 `=== 3` 当触发判据，读-改-写之间只要有一处能被插
     * 进来，这个判据就既会漏也会重（见文件头）。
     */
    bump(chatId: string, contentHash: string): Promise<number>;
}

/** 拆分前就是这个键名。 */
export function repeatCounterKey(chatId: string): string {
    return `repeat_msg:${chatId}`;
}

/** 7 天。拆分前就是 `7 * 24 * 60 * 60`。 */
export const REPEAT_COUNTER_TTL_SECONDS = 7 * 24 * 60 * 60;

/**
 * 读-改-写一次做完。
 *
 * KEYS[1] 计数器的键，ARGV[1] 这条内容的哈希，ARGV[2] 会话 id（只是原样写回记录里，
 * 与拆分前存的那个 JSON 保持一致），ARGV[3] 过期秒数。
 *
 * 存的记录仍是 `{chatId, msg, repeatTime}` —— 与 channel-server 那份互读（见文件头）。
 *
 * `cjson.decode` 解不开时**让脚本报错**，而不是悄悄从 1 重新数：拆分前那侧
 * `JSON.parse` 同样会抛，抛出去的结果是引擎记一条 handler_error。一个解不开的值说明有
 * 别的东西在写这个键，静默重置只会让它继续没人发现。
 */
const BUMP_SCRIPT = `
local raw = redis.call('GET', KEYS[1])
local count = 1
if raw then
    local body = cjson.decode(raw)
    if type(body) == 'table' and body.msg == ARGV[1] then
        count = (tonumber(body.repeatTime) or 0) + 1
    end
end
redis.call('SET', KEYS[1],
    cjson.encode({ chatId = ARGV[2], msg = ARGV[1], repeatTime = count }),
    'EX', tonumber(ARGV[3]))
return count
`;

export function redisRepeatCounter(redis: () => RepeatCounterRedis): LarkRepeatCounter {
    return {
        async bump(chatId, contentHash): Promise<number> {
            const count = await redis().evalScript(
                BUMP_SCRIPT,
                1,
                repeatCounterKey(chatId),
                contentHash,
                chatId,
                REPEAT_COUNTER_TTL_SECONDS,
            );
            // Lua 的整数回来是 number，但端口的返回类型是 unknown，而触发判据是一个
            // `=== 3` —— `'3' === 3` 是 false，一个字符串就能让复读整个哑掉。
            return Number(count);
        },
    };
}
