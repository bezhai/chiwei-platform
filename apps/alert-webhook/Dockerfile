FROM golang:1.25-alpine AS builder
WORKDIR /app
ENV GOPROXY=https://goproxy.cn,direct
COPY go.mod ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /bin/alert-webhook ./cmd/alert-webhook

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=builder /bin/alert-webhook /usr/local/bin/alert-webhook
EXPOSE 8080
ENTRYPOINT ["alert-webhook"]
