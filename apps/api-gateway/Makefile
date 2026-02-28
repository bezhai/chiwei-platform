.PHONY: build run test lint clean

BIN := output/api-gateway
MAIN := ./cmd/api-gateway

build:
	go build -o $(BIN) $(MAIN)

run: build
	$(BIN)

test:
	go test ./... -v -count=1

lint:
	go vet ./...

clean:
	rm -rf output/
