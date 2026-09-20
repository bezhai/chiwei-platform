export type AppEnv = {
  Variables: {
    caller: string;
    user: unknown;
    // Structured audit payload stashed by ops handlers; merged into
    // audit_logs.params top-level by the audit middleware.
    // - gateway-rules 写操作：rule_name/reason/before/after/snapshot_version
    // - world-documents：document_path/request_lane/executed_lane/fingerprint/
    //   content_length/outcome（正文不在其中，只有长度）
    // 键名叫 gatewayAudit 是历史遗留，语义已经不止 gateway；改名是另一条改动。
    gatewayAudit: Record<string, unknown>;
  };
};
