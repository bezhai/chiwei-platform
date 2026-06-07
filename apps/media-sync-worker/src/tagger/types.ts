import type { Document } from 'mongodb';

export type TaggerTaskStatus = 'queued' | 'processing' | 'retry' | 'submitted' | 'completed' | 'failed' | 'submit_failed';

export interface TaggerCallbackPayload extends Record<string, unknown> {
    task_id: string;
    status: string;
    rows: Array<Record<string, unknown>>;
    dups?: string[];
}

export interface TaggerTaskDocument extends Document {
    task_id: string;
    paths: string[];
    status: TaggerTaskStatus | string;
    callback_payload?: TaggerCallbackPayload;
    error?: string | null;
    submitted_at?: Date;
    callback_at?: Date;
    created_at: Date;
    updated_at: Date;
}

export interface TaggerImageResultDocument extends Document {
    pixiv_addr: string;
    object_name: string;
    task_id?: string;
    status: TaggerTaskStatus | string;
    result?: Record<string, unknown>;
    error?: string | null;
    queued_at?: Date;
    processing_at?: Date;
    next_attempt_at?: Date;
    attempts?: number;
    submitted_at?: Date;
    completed_at?: Date;
    created_at: Date;
    updated_at: Date;
}
