BINARY  := h10
PKG     := ./cmd/h10
BIN_DIR := bin
DIST    := dist

# Version from an exact git tag (e.g. v0.2.0); otherwise the default in
# internal/cli (cli.Version) is kept. Override: make VERSION=0.2.0
VERSION ?= $(shell git describe --tags --exact-match 2>/dev/null | sed 's/^v//')
LDFLAGS := -s -w
ifneq ($(VERSION),)
LDFLAGS += -X github.com/al-bashkir/polar-h10/internal/cli.Version=$(VERSION)
endif

GO          ?= go
STATICCHECK := $(shell command -v staticcheck 2>/dev/null || echo $(shell $(GO) env GOPATH)/bin/staticcheck)

.PHONY: all build install test race vet staticcheck check test-hardware linux clean help

all: check build ## run all checks, then build

build: ## build bin/h10 for this machine
	$(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(BIN_DIR)/$(BINARY) $(PKG)

install: ## install h10 into $GOBIN / $GOPATH/bin
	$(GO) install -trimpath -ldflags '$(LDFLAGS)' $(PKG)

test: ## unit tests (no hardware needed)
	$(GO) test ./...

race: ## unit tests with the race detector
	$(GO) test -race ./...

vet: ## go vet for macOS and Linux
	$(GO) vet ./...
	GOOS=linux $(GO) vet ./...

staticcheck: ## staticcheck for macOS and Linux (skipped if not installed)
	@if [ -x "$(STATICCHECK)" ]; then \
		$(STATICCHECK) ./... && GOOS=linux $(STATICCHECK) ./...; \
	else \
		echo "staticcheck not found; install: go install honnef.co/go/tools/cmd/staticcheck@latest"; \
	fi

check: vet staticcheck race ## vet, staticcheck and race tests

test-hardware: ## integration test against a worn H10 (H10_DEVICE=<id> optional)
	$(GO) test -tags hardware -count=1 -v ./internal/ble

linux: ## cross-compile Linux binaries into dist/ (no cgo needed)
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(DIST)/$(BINARY)-linux-amd64 $(PKG)
	CGO_ENABLED=0 GOOS=linux GOARCH=arm64 $(GO) build -trimpath -ldflags '$(LDFLAGS)' -o $(DIST)/$(BINARY)-linux-arm64 $(PKG)

clean: ## remove build outputs
	rm -rf $(BIN_DIR) $(DIST)

help: ## list targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'
