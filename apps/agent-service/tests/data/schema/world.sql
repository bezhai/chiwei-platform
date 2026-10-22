CREATE TABLE IF NOT EXISTS data_session_transcript ("session_id" TEXT, "ver" BIGINT, "transcript_json" TEXT, "dedup_hash" TEXT, "created_at" TIMESTAMPTZ DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS ix_data_session_transcript_dedup ON data_session_transcript("dedup_hash");
CREATE INDEX IF NOT EXISTS ix_data_session_transcript_key_ver ON data_session_transcript("session_id", "ver" DESC);
CREATE TABLE IF NOT EXISTS data_thinking_tokens_spent ("lane" TEXT, "actor" TEXT, "round_id" TEXT, "input_tokens" BIGINT, "output_tokens" BIGINT, "total_tokens" BIGINT, "cached_tokens" BIGINT, "model_calls" BIGINT, "observed_at" TEXT, "dedup_hash" TEXT, "created_at" TIMESTAMPTZ DEFAULT now());
CREATE UNIQUE INDEX IF NOT EXISTS ix_data_thinking_tokens_spent_dedup ON data_thinking_tokens_spent("dedup_hash");
