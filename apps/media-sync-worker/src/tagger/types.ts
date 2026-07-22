import type { Document } from 'mongodb';

export type TaggerTaskStatus = 'queued' | 'processing' | 'retry' | 'registering' | 'submitted' | 'requeueing' | 'completed' | 'failed' | 'submit_failed';

export interface TaggerCallbackPayload extends Record<string, unknown> {
    task_id: string;
    status: string;
    rows: Array<Record<string, unknown>>;
    dups?: string[];
}

export interface TaggerTaskDocument extends Document {
    task_id: string;
    paths: string[];
    image_generations?: Record<string, number>;
    image_processing_leases?: Record<string, string>;
    status: TaggerTaskStatus | string;
    callback_payload?: TaggerCallbackPayload;
    error?: string | null;
    registering_at?: Date;
    submitted_at?: Date;
    next_reconcile_at?: Date;
    reconcile_lease_token?: string;
    reconcile_lease_expires_at?: Date;
    reconcile_error?: string | null;
    callback_at?: Date;
    created_at: Date;
    updated_at: Date;
    stale_paths?: string[];
}

export interface TaggerImageResultDocument extends Document {
    pixiv_addr: string;
    object_name: string;
    task_id?: string;
    generation?: number;
    status: TaggerTaskStatus | string;
    result?: Record<string, unknown>;
    error?: string | null;
    queued_at?: Date;
    processing_at?: Date;
    processing_lease_token?: string;
    next_attempt_at?: Date;
    attempts?: number;
    submitted_at?: Date;
    completed_at?: Date;
    projection_status?: 'pending' | 'processing' | 'retry' | 'projected';
    projection_attempts?: number;
    projection_processing_at?: Date;
    projection_lease_token?: string;
    next_projection_at?: Date;
    projected_at?: Date;
    created_at: Date;
    updated_at: Date;
}
