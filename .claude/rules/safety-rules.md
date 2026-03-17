# 安全与工具链规范

## 改动审批

超过 10 行的改动或重要改动，在没有明确许可的情况下不允许直接修改代码。必须先进入 plan 模式出方案，或询问用户是否可以用简单模式直接修改。

## 五条禁令

1. **禁止 kubectl exec 写操作**：kubectl exec 绝对不能用于修改数据库、文件、配置。仅限 read-only 排查（查看 env、cat 配置、logs）。即使用户说"帮我完成"，也不能通过 kubectl exec 跑 CREATE TABLE 或修改数据。
2. **禁止提取生产密钥**：不允许从 pod 中获取密码、secret_key、API key 等凭据用于本地脚本。
3. **禁止绕过 langfuse skill 操作 Langfuse**：Langfuse 的所有操作必须通过 langfuse skill 执行，不能写 ad-hoc 脚本或 kubectl exec 调 API。
4. **禁止绕过已有 skill/工具**：如果已有 skill（如 ops-db）能完成任务，必须用 skill，不允许自己写脚本绕过。
5. **禁止直接使用 curl 调接口**：调用 API 必须通过 `/api-test` skill 的 `scripts/http.sh` 或 Makefile 命令。

## 操作验证

做了操作必须验证结果，用确切证据而不是推断。

## 环境变量

环境变量由 PaaS 管理。直接改 K8s Secret 会被 PaaS 部署覆盖，必须通过 PaaS API 添加 env。
