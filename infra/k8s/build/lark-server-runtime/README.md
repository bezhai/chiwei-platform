# lark-server runtime base image

`apps/lark-server/Dockerfile` uses this image as its runtime stage to avoid
running `apt-get` during every Kaniko business image build.

Build and push:

```bash
docker build \
  -t harbor.local:30002/inner-bot/lark-server-runtime:bookworm-ca-tz-20260427 \
  infra/k8s/build/lark-server-runtime

docker push harbor.local:30002/inner-bot/lark-server-runtime:bookworm-ca-tz-20260427
```

Current pushed image:

```text
harbor.local:30002/inner-bot/lark-server-runtime:bookworm-ca-tz-20260427
sha256:e25a0e85ea61dba92b3df8acaff2e1c532bd2235463302d827ed6f1a4d6101cc
```
