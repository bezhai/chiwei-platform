export interface BalanceResponse {
    data: {
        balance: string;
    };
}

export interface AIKeyInfoResponse {
    code: number;
    message: string;
    data: AIKeyInfo[];
}

export interface AIKeyInfo {
    id: number;
    api_name: string;
    api_key: string;
    allow_save_logs: boolean;
    allow_manage_key: boolean;
    allow_custom_model: boolean;
    limit_cost: number;
    current_cost: number;
    limit_daily_cost: number;
    current_date_cost: number;
    expired_on: number;
}
