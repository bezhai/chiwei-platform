# 测试环境基础设施

跑在 cpu1 宿主机 docker 上，跟现有 prod PG 同部署模式。

## 拉起

```bash
cd infra/test-env
export CHIWEI_TEST_PG_PASSWORD=<set-a-password>
docker compose up -d chiwei-test-postgres

export CHIWEI_TEST_MQ_PASSWORD=<set-a-password>
docker compose up -d chiwei-test-rabbitmq
```

## 验证

```bash
docker exec chiwei-test-postgres pg_isready -U chiwei_test -d chiwei_test
# 期望: /var/run/postgresql:5432 - accepting connections

docker exec chiwei-test-postgres psql -U chiwei_test -d chiwei_test -c '\dt'
# 期望: Did not find any relations.

docker exec chiwei-test-rabbitmq rabbitmq-diagnostics -q ping
# 期望: Ping succeeded

# 浏览器开 http://cpu1:15673 用 chiwei_test / <password> 登录 management UI
```

## 销毁

```bash
docker compose stop chiwei-test-postgres
docker compose rm -f chiwei-test-postgres
docker volume rm chiwei_test_pg_data  # 慎用，会丢测试数据

docker compose stop chiwei-test-rabbitmq
docker compose rm -f chiwei-test-rabbitmq
docker volume rm chiwei_test_mq_data  # 慎用，会丢测试 MQ 数据
```
