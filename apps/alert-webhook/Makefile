APP     := alert-webhook
OUTPUT  := output

.PHONY: build run test lint clean

build:
	go build -o $(OUTPUT)/$(APP) ./cmd/$(APP)

run: build
	$(OUTPUT)/$(APP)

test:
	go test -count=1 ./...

lint:
	go vet ./...

clean:
	rm -rf $(OUTPUT)
