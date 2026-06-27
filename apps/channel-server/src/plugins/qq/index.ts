// QQ 插件入口。import 期自注册：进 ChannelRegistry / 运行时 registry /
// CommandRegistry(channel='qq', 无平台指令) / chat.request 富化器。inbound（custom→
// InboundMessage）/ addressing（私聊总响应、群看 @bot）/ capabilities（出站发回网关）/
// commands=[] / 凭据解释（宽松）五件齐备，QQ 入站出站控制流全部经此插件收口。

import type { ChannelPlugin, OutboundCapabilities } from '@core/ports/channel-plugin';
import { registerPlugin } from '@core/registry/channel-registry';
import { getCommandRegistry } from '@core/registry/command-registry';
import { registerChatRequestEnricher } from '@core/services/ai/reply';
import { registerChannelRuntime } from '@plugins/runtime';
import { qqInbound } from './inbound';
import { qqAddressing } from './addressing';
import { createQqOutboundCapabilities } from './outbound-capabilities';
import { defaultQqOutboundDeps } from './default-outbound-deps';
import { qqParseCredentials } from './bot-identity';
import { enrichQqChatRequest } from './chat-request-enricher';
import { qqRuntime } from './runtime';

const capabilities: OutboundCapabilities = createQqOutboundCapabilities(defaultQqOutboundDeps);

export const qqPlugin: ChannelPlugin = {
    channel: 'qq',
    inbound: qqInbound,
    addressing: qqAddressing,
    capabilities,
    commands: [],
    parseCredentials(blob: unknown): unknown {
        return qqParseCredentials(blob);
    },
};

registerPlugin(qqPlugin);
registerChannelRuntime(qqRuntime);
getCommandRegistry().register(qqPlugin.channel, qqPlugin.commands);
registerChatRequestEnricher(qqPlugin.channel, enrichQqChatRequest);
