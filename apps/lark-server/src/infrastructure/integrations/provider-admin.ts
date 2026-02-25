import { AIKeyInfo, AIKeyInfoResponse, BalanceResponse } from 'types/admin';
import http from '@http/client';

const BASE_ADMIN_URL = 'https://api.302.ai';

export async function getBalance() {
    const response = await http.get<BalanceResponse>(`${BASE_ADMIN_URL}/dashboard/balance`, {
        headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${process.env.AI_PROVIDER_ADMIN_KEY}`,
        },
    });
    if (response.status !== 200) {
        throw new Error('Failed to get balance');
    }
    return response.data;
}

export async function getAIKeyInfo(): Promise<AIKeyInfo[]> {
    const response = await http.get<AIKeyInfoResponse>(`${BASE_ADMIN_URL}/dashboard/api_keys`, {
        headers: {
            'Content-Type': 'application/json',
            Authorization: `Bearer ${process.env.AI_PROVIDER_ADMIN_KEY}`,
        },
    });
    if (response.status !== 200) {
        throw new Error('Failed to get AI key info');
    }
    return response.data.data;
}
