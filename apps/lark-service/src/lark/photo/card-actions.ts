// 图片卡片按钮的回调契约。
//
//     卡片（cards.ts）──把 value 写进按钮──▶ 飞书 ──用户点了──▶ 回调（callback.ts）
//
// ## 这些字面量是线上历史，不是内部命名
//
// 卡片发出去之后**留在飞书的聊天记录里**：上周发的卡片这周还会被人点开、点按钮。改动
// 这三个 type 字符串或者 value 的字段名，等于让所有存量卡片的按钮变成哑巴 —— 回调照常
// 进来，落进"不认识这个动作"的分支，用户点了没有任何反应也没有任何报错。
//
// 契约单独一个文件是为了让两头都指向它：卡片那边写 value、回调这边读 value。各写各的
// 字面量的话，改一处忘一处正是上面那种静默失效。

/** 「换一批」：拿原来的标签重搜一批，延时更新这张卡片。 */
export const UPDATE_PHOTO_CARD = 'update-photo-card';
/** 「查看详情」：把这批图的作者与标签摊开，只给点的人看。 */
export const FETCH_PHOTO_DETAILS = 'fetch-photo-details';
/** 「今日新图」卡片上的「换一批」：换的是同一天里的另一批。 */
export const UPDATE_DAILY_PHOTO_CARD = 'update-daily-photo-card';

export interface UpdatePhotoCardValue {
    type: typeof UPDATE_PHOTO_CARD;
    tags: string[];
}

export interface FetchPhotoDetailsValue {
    type: typeof FETCH_PHOTO_DETAILS;
    /** 卡片上那批图的 pixiv 地址，顺序即卡片上的顺序。 */
    images: string[];
}

export interface UpdateDailyPhotoCardValue {
    type: typeof UPDATE_DAILY_PHOTO_CARD;
    /** 毫秒时间戳。「换一批」要换的还是那一天的图。 */
    start_time: number;
}

export type LarkCardActionValue =
    | UpdatePhotoCardValue
    | FetchPhotoDetailsValue
    | UpdateDailyPhotoCardValue;

/**
 * 飞书推过来的一次卡片交互。
 *
 * 只列本服务真的会读的字段。表单、下拉、勾选那些回调字段一个都没列 —— 我们的卡片上
 * 只有按钮，列了就是凭空多出要维护的契约面。
 */
export interface LarkCardAction {
    action: {
        tag?: string;
        /** 按钮上写的那份 value。**可能没有**（不是我们发的卡片）。 */
        value?: LarkCardActionValue;
    };
    context: {
        /** 卡片本身那条消息的 id。回复详情卡片挂在它上面。 */
        open_message_id: string;
        open_chat_id: string;
    };
    /** 点按钮的人。延时更新和"仅自己可见"都按它定向。 */
    operator: {
        open_id: string;
        union_id?: string;
        user_id?: string;
    };
    /** 延时更新卡片的凭证，一次性的。 */
    token: string;
}
