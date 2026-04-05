# apps/lane-sidecar/Dockerfile
FROM harbor.local:30002/library/golang:1.25-alpine AS builder
WORKDIR /app
ENV GOPROXY=https://goproxy.cn,direct
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /bin/lane-sidecar ./cmd/lane-sidecar

FROM harbor.local:30002/library/alpine:3.21
RUN apk add --no-cache ca-certificates iptables
COPY --from=builder /bin/lane-sidecar /usr/local/bin/lane-sidecar
ENTRYPOINT ["lane-sidecar"]
