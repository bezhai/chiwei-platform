FROM golang:1.25-alpine AS builder
WORKDIR /app
ENV GOPROXY=https://goproxy.cn,direct
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /bin/api-gateway ./cmd/api-gateway

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=builder /bin/api-gateway /usr/local/bin/api-gateway
COPY config/routes.yaml /etc/api-gateway/routes.yaml
EXPOSE 8080
ENTRYPOINT ["api-gateway"]
