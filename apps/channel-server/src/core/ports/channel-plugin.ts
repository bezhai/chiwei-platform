// 平台插件端口(ports & adapters 的 port 一侧)。core 定义这些接口,平台插件
// 实现它们。控制永远是 core → 端口 → 插件;插件绝不把平台原始对象交回 core,
// core 绝不 import 任何平台 SDK。
//
// 一个平台的全部能力(入站 adapter / 寻址 / 出站能力 / 自带指令 / 凭据解释)
// 收进一个 ChannelPlugin,作为 core 与该平台之间唯一的契约面。

import type {
    InboundAdapter,
    AddressingPolicy,
    ContentItem,
    ThreadRef,
} from '@core/channels/contracts';
import type { RuleConfig } from '@core/rules/rule';

// 出站目标的渠道内引用。插件本身已绑定某 channel,故只需渠道内 id;
// 不在 core 里出现裸 id 的语义泄漏——这些 ref 只在 channel-server 出站边界
// (worker / 指令调能力端口时)由 IdentityResolver.toChannel 翻出后传入。
export interface ConversationRef {
    channelId: string;
}
export interface MessageRef {
    channelId: string;
}

// 出站渲染上下文。这些不是消息「内容」(那是 ContentItem[]),而是把 content
// 渲染成平台格式时所需的「外部引用」——它们不属于渠道内 id 命名空间,故不能塞进
// ConversationRef/MessageRef/ThreadRef(那些只承载渠道内裸 id)。
//   imageRegistryId  内容里若含图片占位引用,用这个【全局】id 去查图片注册表
//                    (与产出图片的上游用的同一个 key)。注意:它是全局 id,绝不是
//                    渠道内裸 id——这正是「用裸 id 查注册表必 miss、图片被吞」那类
//                    bug 的根因,所以单独走渲染上下文、不混进渠道内 ref。
//   groupConversationId  群内 mention(把内容里的 @名字 翻成平台 mention 标记)需要
//                        的会话 id(渠道裸 id)。reply 路径只拿得到 ThreadRef(消息
//                        锚点)、拿不到会话 id,故由上下文补。命名刻意中性、不带平台
//                        名——端口契约里不出现任何平台名。非群/不支持 mention 的
//                        channel 不读它即可,字段是可选的纯字符串、不含任何平台结构。
//   resolveMentions  是否做群 mention 解析(私聊场景关掉,与现状 is_p2p 跳过一致)。
// 单个字段可选——非富内容场景 / 不支持的 channel 不读对应字段即可;但 ctx 对象本身
// 在出站调用里必填(见 OutboundCapabilities),无渲染数据时传空对象、绝不传 undefined。
export interface RenderContext {
    imageRegistryId?: string;
    groupConversationId?: string;
    resolveMentions?: boolean;
}

// 平台能力端口。指令通过它操作平台,绝不碰平台原始对象 / SDK。
// 能力是可选的:平台不支持某能力就不实现它(如 QQ 无 recall)——依赖该能力的
// 指令对该平台自然不可用。平台差异 = 能力有没有,不是 flag、不是优雅降级。
//
// sendText/reply 第三参 ctx: RenderContext 必填:承载「渲染富内容所需的平台无关
// 外部引用」(图片注册表 id / 群会话 id),让 core 侧出站方只产出 ContentItem[] +
// 中性 ctx、把平台翻译全留在插件。必填是刻意的——无渲染数据的调用方也要显式传空
// 对象 {},逼出「这条出站到底带不带渲染上下文」的决策,堵死「忘传 ctx 导致图片/
// mention 被静默吞掉」那类回归。ctx 字段一律平台无关命名,不出现任何平台名。
export interface OutboundCapabilities {
    // 在某会话里新发一条(承载富 Content:文本/图片/富文本等)。ctx 携带渲染所需
    // 的外部引用(图片注册表 id / mention 会话 id),无渲染数据时传空对象 {}。
    sendText(conv: ConversationRef, content: ContentItem[], ctx: RenderContext): Promise<MessageRef>;
    // 在某线程/某条消息下回复。无回复语义的平台可让其等价于 sendText。
    reply(thread: ThreadRef, content: ContentItem[], ctx: RenderContext): Promise<MessageRef>;
    // 撤回。可选——平台不支持就不实现。
    recall?(msg: MessageRef): Promise<void>;
}

// 一个平台插件 = 该平台的全部能力打成一个包。
export interface ChannelPlugin {
    channel: string; // "lark" / "qq" / ...
    inbound: InboundAdapter; // 验签 / 握手 / 原始 → 通用消息
    addressing: AddressingPolicy; // 是否需要 bot 响应 {respond, reason}
    capabilities: OutboundCapabilities; // 本平台能做的出站操作
    commands: RuleConfig[]; // 本平台专属指令(经 CommandRegistry 注册)
    // 解释 bot_config.credentials 这一团不透明 blob,取出本平台需要的凭据。
    // core 不解释 credentials 的形状——那是各平台自己的事。
    parseCredentials(blob: unknown): unknown;
}
