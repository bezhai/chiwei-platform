# 测试环境基础设施

跑在 cpu1 宿主机 docker 上，跟现有 prod PG 同部署模式。

## 拉起

```bash
cd infra/test-env
export CHIWEI_TEST_PG_PASSWORD=<set-a-password>
docker compose up -d chiwei-test-postgres
```

## 验证

```bash
docker exec chiwei-test-postgres pg_isready -U chiwei_test -d chiwei_test
# 期望: localhost:5432 - accepting connections

docker exec chiwei-test-postgres psql -U chiwei_test -d chiwei_test -c '\dt'
# 期望: Did not find any relations.
```

## 销毁

```bash
docker compose down chiwei-test-postgres
docker volume rm chiwei_test_pg_data  # 慎用，会丢测试数据
```
