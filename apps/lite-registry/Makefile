BINARY := lite-registry

.PHONY: build run test lint clean

build:
	go build -o output/$(BINARY) ./cmd/lite-registry

run: build
	./output/$(BINARY)

test:
	go test ./... -v -count=1

lint:
	go vet ./...

clean:
	rm -rf output/
