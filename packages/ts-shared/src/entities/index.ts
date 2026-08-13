// 渠道无关的实体。判据不是「两边都在用」，而是「这张表的定义里出现过任何具体
// 渠道的名字或渠道专属字段吗」—— common_* 只有一个中性的 channel 字符串列，
// bot_config 同理，user_blacklist 连 channel 列都没有。各渠道的私有表归各自服务。
//
// 这里只导出实体类本身，不组装 DataSource：注册哪些实体是调用方的决定
// （见 ../persistence/data-source.ts）。

export { CommonUser } from './common-user';
export { CommonConversation } from './common-conversation';
export { CommonMessage, type CommonMessageContent } from './common-message';
export {
    CommonAgentResponse,
    type CommonAgentResponseReply,
    type CommonSafetyResult,
} from './common-agent-response';
export { BotConfig } from './bot-config';
export { CommonBotPresence } from './common-bot-presence';
export {
    LaneRouting,
    LANE_ROUTING_TABLE,
    LANE_ROUTE_TYPE,
    type LaneRouteType,
} from './lane-routing';
export { UserBlacklist } from './user-blacklist';
