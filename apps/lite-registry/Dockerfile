FROM golang:1.25-alpine AS builder

WORKDIR /app

ENV GOPROXY=https://goproxy.cn,direct

COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o lite-registry ./cmd/lite-registry

FROM alpine:3.21

RUN apk add --no-cache ca-certificates

COPY --from=builder /app/lite-registry /usr/local/bin/lite-registry

EXPOSE 8080

ENTRYPOINT ["lite-registry"]
