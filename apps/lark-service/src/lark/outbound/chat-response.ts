// agent-service 发过来的那条出站消息长什么样。
//
// 这是**跨服务、跨语言的线格式**：写的是 agent-service（Python，
// app/runtime/sink_dispatch.py 把一个 Data 序列化之后直接发出来），读的是这里。
// 两边各写一份字段名，编译期对不上，所以字段一律照抄 Python 那边的下划线命名 ——
// 中间加一层驼峰翻译，只会让"哪个名字是线上真的那个"变得需要猜。
//
// 三个 id 字段（message_id / chat_id / root_id）**全都是公共层 id**，不是飞书裸 id。
// 出站第一步就是拿它们去反查飞书坐标（见 deliver.ts）。塞错了会在反查那一步炸，
// 这是刻意的：静默发到错的会话比发不出去严重得多。

/** 一段回复。一次 AI 回答会被切成若干段，每段一条这样的消息。 */
export interface LarkChatResponse {
    /**
     * 这条回复该由哪个渠道发出去。
     *
     * 消费侧据它做 fail-closed 校验（见 response-queue.ts）。共库方案下
     * `common_agent_response` 没有 channel 列，DB 层拦不住越界写入，隔离完全依赖
     * "生产者 rk 分对了 + 消费侧不越界"这两条。
     */
    channel?: string;

    /**
     * 台账（common_agent_response）的行 key。
     *
     * **主动发是 null** —— 赤尾凭生活节奏自己开口，没有对应的一次"回答请求"，
     * 也就没有台账行。所有台账写入都必须先确认它非空。
     */
    session_id: string | null;

    /**
     * 触发这次回复的那条消息的**公共层** id。
     *
     * 主动发时它是一个伪 id（`proactive:<uuid5>`），拿去反查 lark_message 必 miss。
     * 主动发路径因此完全不查它（见 deliver.ts 的反查分支）。
     */
    message_id: string;

    /** 会话的**公共层** id。 */
    chat_id: string;

    is_p2p: boolean;

    /** 话题根消息的公共层 id。 */
    root_id?: string | null;

    /** 这一段的正文，AI 写的原始 markdown。渲染成飞书富文本是本服务的事。 */
    content: string;

    /**
     * 这一段要带出去的图，值是对象存储的**永久句柄**（file_name），不是地址。
     *
     * 图有自己的字段、不在 content 里，是因为上游那边正文由一个对话模型自由生成，
     * 没有任何原样保留的通道 —— 混在正文里的图片引用必然被改写或丢掉，而且不报错。
     *
     * 存句柄而不是 URL：预签名地址只活 1.5 小时，队列却不保证几秒就投到（泳道队列
     * TTL 降级、DLQ 重投都可能隔很久）。签名要在**最靠近发送的那一刻**由本服务现签，
     * 拿一个过期签名发出去的失败是静默的（消息发得出去，图片点开是坏的）。
     *
     * 可选：老消息（DLQ 里躺着的、旧版 agent-service 发的）没有这个字段，缺字段 =
     * 这一段不带图，照常发正文。
     */
    picture_file_names?: string[];

    /** 整轮回答的全文。只有收尾那一段带，用来回填台账的 response_text。 */
    full_content?: string;

    status: 'success' | 'failed';

    error?: string;

    /**
     * agent-service 仍在 body 里回填 lane，但**判 lane 不看这里**：lane 只认 AMQP
     * header（见 @inner/shared/mq-context 的 laneFromMessage，连同"为什么不回落
     * body"）。字段留在类型里是因为线上真的有它，不是因为我们读它。
     */
    lane?: string;

    part_index?: number;
    is_last?: boolean;
    is_proactive?: boolean;

    /** 由 agent-service 按 persona_id 反查填好。优先于台账里那一列。 */
    bot_name?: string;

    /** 出站失败时排查用：主动发没有 session_id，只能靠它定位是哪个人设发不出去。 */
    persona_id?: string;

    /** 发布时刻（ms）。消费侧据它算队列积压。 */
    published_at?: number;
}
