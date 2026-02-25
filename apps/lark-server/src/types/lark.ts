// 发送者相关信息
export interface LarkSender {
    sender_id?: {
        union_id?: string;
        user_id?: string;
        open_id?: string;
    };
    sender_type: string;
    tenant_key?: string;
}

// 提及用户的结构
export interface LarkMention {
    key: string;
    id: {
        union_id?: string;
        user_id?: string;
        open_id?: string;
    };
    name: string;
    tenant_key?: string;
}

// 消息的结构
export interface LarkMessage {
    message_id: string;
    root_id?: string;
    parent_id?: string;
    create_time: string;
    update_time?: string;
    chat_id: string;
    thread_id?: string;
    chat_type: string;
    message_type: string;
    content: string;
    mentions?: LarkMention[];
    user_agent?: string;
}

// Lark 消息接收事件的整体定义
export interface LarkReceiveMessage {
    event_id?: string;
    token?: string;
    create_time?: string;
    event_type?: string;
    tenant_key?: string;
    ts?: string;
    uuid?: string;
    type?: string;
    app_id?: string;
    sender: LarkSender;
    message: LarkMessage;
}

export interface LarkRecalledMessage {
    event_id?: string;
    token?: string;
    create_time?: string;
    event_type?: string;
    tenant_key?: string;
    ts?: string;
    uuid?: string;
    type?: string;
    app_id?: string;
    message_id?: string;
    chat_id?: string;
    recall_time?: string;
    recall_type?: 'message_owner' | 'group_owner' | 'group_manager' | 'enterprise_manager';
}

export interface LarkReceiveUser {
    name?: string;
    user_id?: {
        union_id?: string;
        user_id?: string;
        open_id?: string;
    };
}

export interface LarkGroupMemberChangeInfo {
    users?: Array<LarkReceiveUser>;
    chat_id?: string;
}

export interface LarkGroupChangeInfo {
    chat_id?: string;
}

export interface CardActionTrigger {
    operator?: {
        union_id?: string;
    }; // 回调触发者信息
    token?: string; // 更新卡片用的凭证
    action?: {
        tag?: string; // 交互组件的标签
        value?:
            | {
                  [key: string]: any;
              }
            | string; // 交互组件绑定的开发者自定义回传数据，对应组件中的 value 属性。类型为 string 或 object，可由开发者指定
        name?: string; // 组件的自定义唯一标识，用于识别内嵌在表单容器中的某个组件
        form_value?: {
            [key: string]: any;
        }; // 表单容器内用户提交的数据
        input_value?: string; // 当输入框组件未内嵌在表单容器中时，用户在输入框中提交的数据
        option?: string; // 当折叠按钮组、下拉选择-单选、人员选择-单选、日期选择器、时间选择器、日期时间选择器组件未内嵌在表单容器中时，用户选择该类组件某个选项时，组件返回的选项回调值
        options?: string[]; // 当下拉选择-多选组件和人员选择-多选组件未内嵌在表单容器中时，用户选择该类组件某个选项时，组件返回的选项回调值
        checked?: boolean; // 当勾选器组件未内嵌在表单容器中时，勾选器组件的回调数据
        context?: {
            open_message_id?: string;
            open_chat_id?: string;
        };
    };
}

export interface LarkHistoryMessage {
    message_id?: string;
    root_id?: string;
    parent_id?: string;
    create_time?: string;
    update_time?: string;
    chat_id?: string;
    thread_id?: string;
    msg_type?: string;
    deleted?: boolean;
    updated?: boolean;
    body?: { content: string };
    mentions?: {
        key: string;
        id: string;
        id_type: string;
        name: string;
        tenant_key?: string;
    }[];
    sender?: {
        id?: string;
        id_type?: string;
        sender_type?: string; // app_id or open_id
    };
}

export interface LarkHistoryMessageResp {
    has_more?: boolean;
    items?: Array<LarkHistoryMessage>;
    page_token?: string;
}

export const UpdatePhotoCard = 'update-photo-card';
export const FetchPhotoDetails = 'fetch-photo-details';
export const UpdateDailyPhotoCard = 'update-daily-photo-card';
export const LarkCardThumbsUp = 'lark_card_thumbs_up';
export const LarkCardThumbsDown = 'lark_card_thumbs_down';
export const LarkCardRetry = 'lark_card_retry';

export interface UpdatePhotoCardCallback {
    type: typeof UpdatePhotoCard;
    tags: string[];
}

export interface UpdateDailyPhotoCardCallback {
    type: typeof UpdateDailyPhotoCard;
    start_time: number;
}

export interface FetchPhotoDetailsCallback {
    type: typeof FetchPhotoDetails;
    images: string[];
}

export interface LarkCardThumbsUpCallback {
    type: typeof LarkCardThumbsUp;
    message_id: string;
    chat_id: string;
    parent_message_id: string;
    union_id: string;
}

export interface LarkCardThumbsDownCallback {
    type: typeof LarkCardThumbsDown;
    message_id: string;
    chat_id: string;
    parent_message_id: string;
    union_id: string;
}

export interface LarkCardRetryCallback {
    type: typeof LarkCardRetry;
    message_id: string;
    chat_id: string;
    root_id: string;
    is_p2p: boolean;
    parent_message_id: string;
}

type LarkCallbackValue =
    | UpdatePhotoCardCallback
    | FetchPhotoDetailsCallback
    | UpdateDailyPhotoCardCallback
    | LarkCardThumbsUpCallback
    | LarkCardThumbsDownCallback
    | LarkCardRetryCallback;

export interface LarkCallbackInfo {
    action: {
        tag: string; // 交互组件的标签
        value?: LarkCallbackValue;
        name?: string; // 组件的自定义唯一标识，用于识别内嵌在表单容器中的某个组件
        form_value?: { [key: string]: any }; // 表单容器内用户提交的数据
        input_value?: string; // 当输入框组件未内嵌在表单容器中时，用户在输入框中提交的数据
        option?: string; // 当折叠按钮组、下拉选择-单选、人员选择-单选、日期选择器、时间选择器、日期时间选择器组件未内嵌在表单容器中时，用户选择该类组件某个选项时，组件返回的选项回调值
        options?: string[]; // 当下拉选择-多选组件和人员选择-多选组件未内嵌在表单容器中时，用户选择该类组件某个选项时，组件返回的选项回调值
        checked?: boolean; // 当勾选器组件未内嵌在表单容器中时，勾选器组件的回调数据
    };
    host: string; // 卡片展示场景, 已知有im_message
    context: {
        open_message_id: string;
        open_chat_id: string;
    };
    operator: {
        union_id: string;
        user_id: string;
        open_id: string;
    };
    token: string; // 更新卡片用的凭证
}

export interface LarkOperateReactionInfo {
    message_id?: string;
    reaction_type?: {
        emoji_type?: string;
    };
    event_id?: string;
    event_type?: string;
    operator_type?: {
        open_id?: string;
    };
    user_id?: {
        union_id?: string;
        user_id?: string;
        open_id?: string;
    };
    app_id?: string;
    action_time?: string;
}

export type AddElementType =
    | 'insert_before' // 在目标组件前插入
    | 'insert_after' // 在目标组件后插入
    | 'append'; // 在卡片或容器组件末尾添加

export interface LarkEnterChatEvent {
    event_id?: string;
    token?: string;
    create_time?: string;
    event_type?: string;
    tenant_key?: string;
    ts?: string;
    uuid?: string;
    type?: string;
    app_id?: string;
    chat_id?: string;
    operator_id?: {
        union_id?: string;
        user_id?: string;
        open_id?: string;
    };
    last_message_id?: string;
    last_message_create_time?: string;
}
