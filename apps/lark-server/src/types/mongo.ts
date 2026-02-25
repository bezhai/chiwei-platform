export type LarkMessageMetaInfo = LarkUserMessageMetaInfo | LarkRobotMessageMetaInfo;

export interface LarkBaseMessageMetaInfo {
    message_id: string; // 消息 ID
    root_id?: string; // 根消息 ID
    parent_id?: string; // 父消息 ID
    chat_id: string; // 会话 ID
    thread_id?: string; // 话题 ID
    chat_type: string; // 会话类型
    is_delete: boolean; // 是否被撤回
    create_time: Date; // 创建时间
    update_time: Date; // 更新时间
    message_type: string; // 消息类型
}

export interface LarkUserMessageMetaInfo extends LarkBaseMessageMetaInfo {
    sender?: string; // 发送者 ID, 如果是机器人则没有该字段
    is_from_robot: false; // 是否是机器人发送的消息
    message_content?: string; // 消息内容
    mentions: LarkSimpleMention[]; // 提及的用户 ID 列表, 这里是 open_id
}

export interface LarkRobotMessageMetaInfo extends LarkBaseMessageMetaInfo {
    is_from_robot: true; // 是否是机器人发送的消息
    robot_text?: string; // 机器人回复的内容
    card_id?: string; // 机器人回复的卡片 ID
}

export interface LarkSimpleMention {
    key: string;
    id: {
        union_id?: string;
        user_id?: string;
        open_id?: string;
    };
    name: string;
}
