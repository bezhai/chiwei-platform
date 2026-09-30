export type AppEnv = {
  Variables: {
    caller: string;
    user: unknown;
    // Structured audit payload stashed by ops handlers; merged into
    // audit_logs.params top-level by the audit middleware.
    // - gateway-rules 写操作：rule_name/reason/before/after/snapshot_version
    // - messaging：request_lane/executed_lane/message_id/outcome
    // - world 记录：request_lane/executed_lane/record_path/fingerprint_before/fingerprint_after/outcome
    // 键名叫 gatewayAudit 是历史遗留，语义已经不止 gateway；改名是另一条改动。
    gatewayAudit: Record<string, unknown>;
    // The route already wrote this request's audit row itself (world record writes
    // and deletes write it before forwarding); the audit middleware skips it.
    auditWritten: boolean;
  };
};
