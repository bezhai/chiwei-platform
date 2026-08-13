import { context as baseContext, type BaseRequestContext } from '@inner/shared';
import { v4 as uuidv4 } from 'uuid';

// 本服务的请求上下文工具。取值口径（getTraceId / getBotName / getLane / get /
// getAll / set / run）全部定义在 @inner/shared 的基座 context，这里一条都不重复
// —— 只补一个本服务调用顺序的 createContext。
//
// 上下文的形状本身也在基座定义（BaseRequestContext 已带 botName / lane），本服务
// 没有额外字段，故直接用它。

export type RequestContext = BaseRequestContext;

// trace / bot-context 中间件直接操作 AsyncLocalStorage，从基座取同一个实例
// （全进程必须只有一个，否则 run 进去的上下文另一侧读不到）。
export { asyncLocalStorage } from '@inner/shared';

export const context = {
    ...baseContext,
    /**
     * 创建带 traceId / botName / lane 的上下文数据。形参顺序是本服务入站链路的
     * 调用习惯（botName 优先），与基座的 createContext(traceId, extra) 不同，故在
     * 这里单独定义而不是复用基座那个。
     */
    createContext: (botName?: string, traceId?: string, lane?: string): RequestContext => {
        return {
            traceId: traceId || uuidv4(),
            botName,
            lane,
        };
    },
};
