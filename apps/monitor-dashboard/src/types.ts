export type AppEnv = {
  Variables: {
    caller: string;
    user: unknown;
    // Structured audit payload stashed by ops handlers; merged into
    // audit_logs.params top-level by the audit middleware. Fixed keys for
    // gateway-rules write ops: rule_name/reason/before/after/snapshot_version.
    gatewayAudit: Record<string, unknown>;
  };
};
