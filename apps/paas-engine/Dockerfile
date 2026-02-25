FROM golang:1.25-alpine AS builder
WORKDIR /app
ENV GOPROXY=https://goproxy.cn,direct
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /bin/paas-engine ./cmd/paas-engine

FROM alpine:3.21
RUN apk add --no-cache ca-certificates
COPY --from=builder /bin/paas-engine /usr/local/bin/paas-engine
EXPOSE 8080
ENTRYPOINT ["paas-engine"]
