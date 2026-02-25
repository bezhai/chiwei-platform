.PHONY: build run test lint clean

BIN := output/paas-engine
MAIN := ./cmd/paas-engine

build:
	go build -o $(BIN) $(MAIN)

run: build
	DATABASE_URL="postgres://paas:paas@localhost:5432/paas_engine?sslmode=disable" $(BIN)

test:
	go test ./... -v -count=1

lint:
	go vet ./...

clean:
	rm -rf output/
