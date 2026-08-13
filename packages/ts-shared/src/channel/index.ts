// 渠道接入的通用契约面。这里出现的每一个名字都必须换成任意一个新渠道后仍然
// 讲得通 —— 包内不认识任何具体渠道。

export type {
    AddressingDecision,
    AddressingHint,
    AddressingPolicy,
    ContentItem,
    InboundAdapter,
    InboundMessage,
    ThreadRef,
} from './contracts';
export { assertValidInboundMessage, enforceDecision } from './contracts';

export type {
    ChannelPlugin,
    CommonMessageResolveInput,
    ConversationRef,
    MessageRef,
    OutboundCapabilities,
    OutboundMessageRecordInput,
    OutboundResolvedTarget,
    OutboundTargetResolveInput,
    RenderContext,
} from './plugin';

export { ChannelRegistry, getChannelRegistry, registerPlugin } from './channel-registry';
