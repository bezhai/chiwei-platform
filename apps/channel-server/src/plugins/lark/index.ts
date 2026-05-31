// 飞书插件入口。import 期自注册：把自己注册进 ChannelRegistry 单例、把 10 条
// 平台指令注册进 CommandRegistry 单例(channel='lark')。inbound（入站握手/验签/
// parse）/ addressing（寻址判定）/ capabilities（出站能力）/ commands / 凭据
// 解释五件齐备，飞书入站出站控制流全部经此插件收口。

import type {
    ChannelPlugin,
    OutboundCapabilities,
} from '@core/ports/channel-plugin';
import { registerPlugin } from '@core/registry/channel-registry';
import { larkInbound } from './inbound';
import { larkAddressing } from './addressing';
import { getCommandRegistry } from '@core/registry/command-registry';
import { setUtilityRedirectResponder } from '@core/rules/engine';
import { setChatRequestEnricher } from '@core/services/ai/reply';
import { larkCommands } from './commands';
import { sendLarkUtilityRedirect } from './utility-redirect';
import { enrichLarkChatRequest } from './chat-request-enricher';
import { createLarkOutboundCapabilities } from './outbound-capabilities';
import { defaultLarkOutboundDeps } from './default-outbound-deps';

// 出站能力：飞书富文本/图片回复 + 撤回收进能力端口。chat-response-worker
// / recall-worker 统一走这里，不再各自 import @lark/basic/message / @lark-client。
// 渲染管线（@N.png 上传飞书、@用户名 mention、markdown→PostContent、send/reply/
// delete）在 outbound-capabilities 实现一次；defaultLarkOutboundDeps 接真实飞书
// SDK / redis / DB。
const capabilities: OutboundCapabilities = createLarkOutboundCapabilities(
    defaultLarkOutboundDeps,
);

export const larkPlugin: ChannelPlugin = {
    channel: 'lark',
    inbound: larkInbound,
    addressing: larkAddressing,
    capabilities,
    commands: larkCommands,
    // 解释 bot_config.credentials：目前飞书直接透传 blob，由消费方按 lark
    // app_id/app_secret/robot_union_id 解释（见 lark-credentials.ts）。
    parseCredentials(blob: unknown): unknown {
        return blob;
    },
};

// import 期自注册副作用：插件进 ChannelRegistry、指令进 CommandRegistry、
// utility-redirect 引导提示的飞书实现注入 engine 的中性 responder 注入点
// （engine 不认识飞书 SDK，发法由本插件提供）。plugins/index.ts import 本
// 模块即触发。重复注册由各注册表 fail-closed 兜底。
registerPlugin(larkPlugin);
getCommandRegistry().register(larkPlugin.channel, larkPlugin.commands);
setUtilityRedirectResponder(sendLarkUtilityRedirect);
setChatRequestEnricher(enrichLarkChatRequest);
