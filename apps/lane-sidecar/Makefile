.PHONY: build test lint

build:
	go build -o output/lane-sidecar ./cmd/lane-sidecar

test:
	go test ./... -v -count=1

lint:
	go vet ./...
