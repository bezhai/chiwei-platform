# Sidecar 泳道路由 Step 1：基础设施就绪

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 sidecar 透明代理 + PaaS Engine 注入能力 + 三端（TS/Python/Go）通用上下文传播中间件，此阶段不影响现有服务。

**Architecture:** Go sidecar 通过 iptables 透明拦截 Pod 内出站 HTTP 流量，读取 `x-ctx-lane` header 路由到对应泳道实例。通用上下文传播中间件在框架级自动透传 `x-ctx-*` 前缀 header。PaaS Engine 部署时按 app 配置自动注入 sidecar + init container。

**Tech Stack:** Go 1.25, Hono (TS), FastAPI (Python), chi (Go HTTP router), iptables, Prometheus metrics

**Spec:** `docs/superpowers/specs/2026-04-04-sidecar-lane-routing-design.md`

---

## File Structure

### New files
```
apps/lane-sidecar/
  cmd/lane-sidecar/main.go          # 入口：--init 模式 (iptables) 或 proxy 模式
  internal/
    proxy/
      server.go                      # TCP listener + 协议检测 + HTTP 反向代理
      server_test.go
      originaldst_linux.go           # SO_ORIGINAL_DST (Linux)
      originaldst_other.go           # Stub (非 Linux，开发用)
    registry/
      client.go                      # lite-registry 轮询客户端
      client_test.go
    iptables/
      setup.go                       # iptables 规则生成与执行
  go.mod
  go.sum
  Dockerfile
  Makefile

packages/ts-shared/src/middleware/
  context-propagation.ts             # x-ctx-* Hono 中间件 + 出站 hook
  context-propagation.test.ts        # 测试

packages/py-shared/inner_shared/middlewares/
  context_propagation.py             # x-ctx-* FastAPI 中间件 + httpx hook
  test_context_propagation.py        # 测试
```

### Modified files
```
apps/paas-engine/internal/domain/app.go              # 加 SidecarEnabled 字段
apps/paas-engine/internal/adapter/repository/model.go # 加 SidecarEnabled 列
apps/paas-engine/internal/adapter/repository/app_repo.go # toDomain/toModel 映射
apps/paas-engine/internal/service/app_service.go      # UpdateApp 支持 sidecar_enabled
apps/paas-engine/internal/adapter/kubernetes/deployer.go # 注入 sidecar + init container
apps/paas-engine/internal/adapter/http/middleware.go  # 加 context propagation middleware
apps/paas-engine/internal/adapter/http/router.go      # 挂载新 middleware
packages/ts-shared/src/middleware/index.ts            # 导出新中间件
packages/py-shared/inner_shared/middlewares/__init__.py # 导出新中间件
```

---

## Task 1: Go sidecar — lite-registry 轮询客户端

**Files:**
- Create: `apps/lane-sidecar/go.mod`
- Create: `apps/lane-sidecar/internal/registry/client.go`
- Create: `apps/lane-sidecar/internal/registry/client_test.go`

- [ ] **Step 1: 初始化 Go module**

```bash
cd apps/lane-sidecar
go mod init github.com/chiwei-platform/lane-sidecar
```

- [ ] **Step 2: 写 registry client 测试**

```go
// apps/lane-sidecar/internal/registry/client_test.go
package registry

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestClient_Lookup(t *testing.T) {
	routes := map[string]any{
		"services": map[string]any{
			"agent-service": map[string]any{
				"lanes": []string{"dev", "feat-test"},
				"port":  8000,
			},
			"lark-server": map[string]any{
				"lanes": []string{"dev"},
				"port":  3000,
			},
		},
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(routes)
	}))
	defer srv.Close()

	c := NewClient(srv.URL, 100*time.Millisecond)
	defer c.Stop()

	// 等待第一次轮询
	time.Sleep(200 * time.Millisecond)

	// 已知服务 + 已知泳道
	info, ok := c.Lookup("agent-service")
	if !ok {
		t.Fatal("expected agent-service to be found")
	}
	if info.Port != 8000 {
		t.Fatalf("expected port 8000, got %d", info.Port)
	}
	if !info.HasLane("feat-test") {
		t.Fatal("expected feat-test lane")
	}
	if info.HasLane("staging") {
		t.Fatal("did not expect staging lane")
	}

	// 未知服务
	_, ok = c.Lookup("unknown-service")
	if ok {
		t.Fatal("did not expect unknown-service")
	}
}

func TestClient_ResolveHost(t *testing.T) {
	routes := map[string]any{
		"services": map[string]any{
			"agent-service": map[string]any{
				"lanes": []string{"dev"},
				"port":  8000,
			},
		},
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(routes)
	}))
	defer srv.Close()

	c := NewClient(srv.URL, 100*time.Millisecond)
	defer c.Stop()
	time.Sleep(200 * time.Millisecond)

	tests := []struct {
		host string
		lane string
		want string
	}{
		{"agent-service:8000", "dev", "agent-service-dev:8000"},
		{"agent-service:8000", "prod", "agent-service:8000"},    // prod 不改
		{"agent-service:8000", "", "agent-service:8000"},        // 无 lane 不改
		{"agent-service:8000", "staging", "agent-service:8000"}, // 泳道不存在 fallback
		{"external-api.com:443", "dev", "external-api.com:443"}, // 非集群服务不改
	}
	for _, tt := range tests {
		got := c.ResolveHost(tt.host, tt.lane)
		if got != tt.want {
			t.Errorf("ResolveHost(%q, %q) = %q, want %q", tt.host, tt.lane, got, tt.want)
		}
	}
}
```

- [ ] **Step 3: 运行测试，确认失败**

```bash
cd apps/lane-sidecar && go test ./internal/registry/ -v
```
Expected: 编译失败，`NewClient`、`ServiceInfo`、`HasLane`、`ResolveHost` 未定义。

- [ ] **Step 4: 实现 registry client**

```go
// apps/lane-sidecar/internal/registry/client.go
package registry

import (
	"encoding/json"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"
)

// ServiceInfo 存储某个服务的路由信息。
type ServiceInfo struct {
	Lanes []string `json:"lanes"`
	Port  int      `json:"port"`
}

// HasLane 检查是否存在指定泳道。
func (s *ServiceInfo) HasLane(lane string) bool {
	for _, l := range s.Lanes {
		if l == lane {
			return true
		}
	}
	return false
}

type routesResponse struct {
	Services map[string]ServiceInfo `json:"services"`
}

// Client 轮询 lite-registry 并缓存路由表。
type Client struct {
	registryURL string
	httpClient  *http.Client

	mu       sync.RWMutex
	services map[string]ServiceInfo

	stopCh chan struct{}
}

// NewClient 创建 registry client 并启动后台轮询。
func NewClient(registryURL string, pollInterval time.Duration) *Client {
	c := &Client{
		registryURL: strings.TrimRight(registryURL, "/"),
		httpClient:  &http.Client{Timeout: 5 * time.Second},
		services:    make(map[string]ServiceInfo),
		stopCh:      make(chan struct{}),
	}
	go c.poll(pollInterval)
	return c
}

func (c *Client) poll(interval time.Duration) {
	c.fetch() // 立即拉取一次
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-ticker.C:
			c.fetch()
		case <-c.stopCh:
			return
		}
	}
}

func (c *Client) fetch() {
	resp, err := c.httpClient.Get(c.registryURL + "/v1/routes")
	if err != nil {
		log.Printf("[registry] fetch error: %v", err)
		return
	}
	defer resp.Body.Close()

	var data routesResponse
	if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
		log.Printf("[registry] decode error: %v", err)
		return
	}

	c.mu.Lock()
	c.services = data.Services
	c.mu.Unlock()
}

// Lookup 返回服务的路由信息。
func (c *Client) Lookup(service string) (ServiceInfo, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	info, ok := c.services[service]
	return info, ok
}

// ResolveHost 根据 lane 解析目标 host。
// 输入: "agent-service:8000", "dev" → "agent-service-dev:8000"
// 如果 lane 为空、为 "prod"、泳道不存在或服务未知，返回原始 host。
func (c *Client) ResolveHost(host, lane string) string {
	if lane == "" || lane == "prod" {
		return host
	}

	serviceName, port := splitHostPort(host)
	info, ok := c.Lookup(serviceName)
	if !ok || !info.HasLane(lane) {
		return host
	}

	if port != "" {
		return serviceName + "-" + lane + ":" + port
	}
	return serviceName + "-" + lane
}

// Stop 停止后台轮询。
func (c *Client) Stop() {
	close(c.stopCh)
}

func splitHostPort(host string) (string, string) {
	idx := strings.LastIndex(host, ":")
	if idx < 0 {
		return host, ""
	}
	return host[:idx], host[idx+1:]
}
```

- [ ] **Step 5: 运行测试，确认通过**

```bash
cd apps/lane-sidecar && go test ./internal/registry/ -v
```
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add apps/lane-sidecar/
git commit -m "feat(lane-sidecar): add lite-registry polling client"
```

---

## Task 2: Go sidecar — 透明代理核心

**Files:**
- Create: `apps/lane-sidecar/internal/proxy/originaldst_linux.go`
- Create: `apps/lane-sidecar/internal/proxy/originaldst_other.go`
- Create: `apps/lane-sidecar/internal/proxy/server.go`
- Create: `apps/lane-sidecar/internal/proxy/server_test.go`

- [ ] **Step 1: 写 SO_ORIGINAL_DST 平台实现**

```go
// apps/lane-sidecar/internal/proxy/originaldst_linux.go
//go:build linux

package proxy

import (
	"fmt"
	"net"
	"syscall"
	"unsafe"
)

// GetOriginalDst 通过 SO_ORIGINAL_DST 获取 iptables REDIRECT 前的原始目标地址。
func GetOriginalDst(conn *net.TCPConn) (net.Addr, error) {
	raw, err := conn.SyscallConn()
	if err != nil {
		return nil, err
	}

	var addr *syscall.RawSockaddrInet4
	var callErr error

	err = raw.Control(func(fd uintptr) {
		var rawAddr syscall.RawSockaddrInet4
		addrLen := uint32(unsafe.Sizeof(rawAddr))
		_, _, errno := syscall.Syscall6(
			syscall.SYS_GETSOCKOPT,
			fd,
			syscall.SOL_IP,
			80, // SO_ORIGINAL_DST
			uintptr(unsafe.Pointer(&rawAddr)),
			uintptr(unsafe.Pointer(&addrLen)),
			0,
		)
		if errno != 0 {
			callErr = fmt.Errorf("getsockopt SO_ORIGINAL_DST: %w", errno)
			return
		}
		addr = &rawAddr
	})
	if err != nil {
		return nil, err
	}
	if callErr != nil {
		return nil, callErr
	}

	ip := net.IPv4(addr.Addr[0], addr.Addr[1], addr.Addr[2], addr.Addr[3])
	port := int(addr.Port>>8) | int(addr.Port&0xff)<<8 // network byte order
	return &net.TCPAddr{IP: ip, Port: port}, nil
}
```

```go
// apps/lane-sidecar/internal/proxy/originaldst_other.go
//go:build !linux

package proxy

import (
	"fmt"
	"net"
)

// GetOriginalDst 非 Linux 环境的 stub，用于开发和测试。
func GetOriginalDst(conn *net.TCPConn) (net.Addr, error) {
	return nil, fmt.Errorf("SO_ORIGINAL_DST not supported on this platform")
}
```

- [ ] **Step 2: 写代理服务器测试**

测试核心路由逻辑，不依赖 iptables/SO_ORIGINAL_DST。

```go
// apps/lane-sidecar/internal/proxy/server_test.go
package proxy

import (
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/chiwei-platform/lane-sidecar/internal/registry"
)

// mockRegistry 实现 registry.Resolver 接口用于测试。
type mockRegistry struct {
	services map[string]registry.ServiceInfo
}

func (m *mockRegistry) Lookup(service string) (registry.ServiceInfo, bool) {
	info, ok := m.services[service]
	return info, ok
}

func (m *mockRegistry) ResolveHost(host, lane string) string {
	if lane == "" || lane == "prod" {
		return host
	}
	svc, port := splitHost(host)
	info, ok := m.services[svc]
	if !ok || !info.HasLane(lane) {
		return host
	}
	if port != "" {
		return svc + "-" + lane + ":" + port
	}
	return svc + "-" + lane
}

func splitHost(host string) (string, string) {
	for i := len(host) - 1; i >= 0; i-- {
		if host[i] == ':' {
			return host[:i], host[i+1:]
		}
	}
	return host, ""
}

func TestHandler_RoutesToLaneInstance(t *testing.T) {
	// 模拟泳道实例后端
	laneBackend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("lane-response"))
	}))
	defer laneBackend.Close()

	reg := &mockRegistry{
		services: map[string]registry.ServiceInfo{
			"agent-service": {Lanes: []string{"dev"}, Port: 8000},
		},
	}

	handler := NewHandler(reg, func(host string) string {
		// 测试时把 agent-service-dev:8000 映射到实际 test server
		return laneBackend.Listener.Addr().String()
	})

	// 请求带 x-ctx-lane: dev，Host: agent-service:8000
	req := httptest.NewRequest("GET", "http://agent-service:8000/api/chat", nil)
	req.Header.Set("x-ctx-lane", "dev")
	w := httptest.NewRecorder()

	handler.ServeHTTP(w, req)

	resp := w.Result()
	body, _ := io.ReadAll(resp.Body)
	if string(body) != "lane-response" {
		t.Fatalf("expected 'lane-response', got %q", string(body))
	}
}

func TestHandler_FallbackWhenNoLane(t *testing.T) {
	prodBackend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("prod-response"))
	}))
	defer prodBackend.Close()

	reg := &mockRegistry{
		services: map[string]registry.ServiceInfo{
			"agent-service": {Lanes: []string{"dev"}, Port: 8000},
		},
	}

	handler := NewHandler(reg, func(host string) string {
		return prodBackend.Listener.Addr().String()
	})

	// 无 x-ctx-lane header
	req := httptest.NewRequest("GET", "http://agent-service:8000/api/chat", nil)
	w := httptest.NewRecorder()

	handler.ServeHTTP(w, req)

	body, _ := io.ReadAll(w.Result().Body)
	if string(body) != "prod-response" {
		t.Fatalf("expected 'prod-response', got %q", string(body))
	}
}

func TestHandler_ExternalTrafficPassthrough(t *testing.T) {
	externalBackend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("external-response"))
	}))
	defer externalBackend.Close()

	reg := &mockRegistry{
		services: map[string]registry.ServiceInfo{},
	}

	handler := NewHandler(reg, func(host string) string {
		return externalBackend.Listener.Addr().String()
	})

	req := httptest.NewRequest("GET", "http://api.openai.com/v1/chat", nil)
	req.Header.Set("x-ctx-lane", "dev")
	w := httptest.NewRecorder()

	handler.ServeHTTP(w, req)

	body, _ := io.ReadAll(w.Result().Body)
	if string(body) != "external-response" {
		t.Fatalf("expected 'external-response', got %q", string(body))
	}
}
```

- [ ] **Step 3: 运行测试，确认失败**

```bash
cd apps/lane-sidecar && go test ./internal/proxy/ -v
```
Expected: 编译失败，`NewHandler` 等未定义。

- [ ] **Step 4: 实现代理服务器**

```go
// apps/lane-sidecar/internal/proxy/server.go
package proxy

import (
	"context"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strings"
	"time"

	"github.com/chiwei-platform/lane-sidecar/internal/registry"
)

// Resolver 是路由表查询接口。
type Resolver interface {
	Lookup(service string) (registry.ServiceInfo, bool)
	ResolveHost(host, lane string) string
}

// HostMapper 将逻辑 host (如 "agent-service-dev:8000") 映射到实际地址。
// 生产环境直接返回原值（靠 K8s DNS 解析），测试时映射到 mock server。
type HostMapper func(host string) string

// DefaultHostMapper 生产环境使用，原样返回。
func DefaultHostMapper(host string) string { return host }

// Handler 是 sidecar 的 HTTP 请求处理器。
type Handler struct {
	resolver   Resolver
	hostMapper HostMapper
}

// NewHandler 创建 HTTP handler。
func NewHandler(resolver Resolver, hostMapper HostMapper) *Handler {
	if hostMapper == nil {
		hostMapper = DefaultHostMapper
	}
	return &Handler{resolver: resolver, hostMapper: hostMapper}
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	lane := r.Header.Get("x-ctx-lane")
	targetHost := h.resolver.ResolveHost(r.Host, lane)
	actualHost := h.hostMapper(targetHost)

	target := &url.URL{
		Scheme: "http",
		Host:   actualHost,
	}

	proxy := &httputil.ReverseProxy{
		Director: func(req *http.Request) {
			req.URL.Scheme = target.Scheme
			req.URL.Host = target.Host
			req.Host = r.Host // 保留原始 Host header
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Printf("[proxy] error forwarding to %s: %v", actualHost, err)
			http.Error(w, "sidecar proxy error", http.StatusBadGateway)
		},
	}
	proxy.ServeHTTP(w, r)
}

// Server 是 sidecar 的 TCP 服务器，支持 HTTP 和非 HTTP 流量。
type Server struct {
	handler    *Handler
	listenAddr string
	httpServer *http.Server
}

// NewServer 创建 sidecar server。
func NewServer(listenAddr string, resolver Resolver) *Server {
	handler := NewHandler(resolver, nil)
	s := &Server{
		handler:    handler,
		listenAddr: listenAddr,
	}
	s.httpServer = &http.Server{
		Handler:      handler,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
	}
	return s
}

// ListenAndServe 启动 sidecar 监听。
func (s *Server) ListenAndServe() error {
	ln, err := net.Listen("tcp", s.listenAddr)
	if err != nil {
		return err
	}
	log.Printf("[proxy] listening on %s", s.listenAddr)
	return s.httpServer.Serve(ln)
}

// Shutdown 优雅关闭。
func (s *Server) Shutdown(ctx context.Context) error {
	return s.httpServer.Shutdown(ctx)
}
```

- [ ] **Step 5: registry client 需要导出 Resolver 接口**

在 `apps/lane-sidecar/internal/registry/client.go` 顶部加：

```go
// Resolver 定义路由查询接口，方便测试 mock。
type Resolver interface {
	Lookup(service string) (ServiceInfo, bool)
	ResolveHost(host, lane string) string
}
```

并确保 `Client` 实现了 `Resolver`（它已有 `Lookup` 和 `ResolveHost` 方法）。

同时更新 `proxy/server.go` 中的 `Resolver` 引用——直接使用 `registry.Resolver`：

```go
// 修改 server.go 中的 Resolver 定义
// 删除 proxy 包中的 Resolver interface，改为使用 registry.Resolver
import "github.com/chiwei-platform/lane-sidecar/internal/registry"
```

Handler 和 Server 中的 `Resolver` 改为 `registry.Resolver`。

- [ ] **Step 6: 运行测试，确认通过**

```bash
cd apps/lane-sidecar && go test ./... -v
```
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add apps/lane-sidecar/
git commit -m "feat(lane-sidecar): implement transparent HTTP proxy with lane routing"
```

---

## Task 3: Go sidecar — iptables init 模式 + Dockerfile

**Files:**
- Create: `apps/lane-sidecar/internal/iptables/setup.go`
- Create: `apps/lane-sidecar/cmd/lane-sidecar/main.go`
- Create: `apps/lane-sidecar/Dockerfile`
- Create: `apps/lane-sidecar/Makefile`

- [ ] **Step 1: 实现 iptables 规则生成**

```go
// apps/lane-sidecar/internal/iptables/setup.go
package iptables

import (
	"fmt"
	"os/exec"
	"strings"
)

const (
	ProxyUID  = 1337
	ProxyPort = 15001
)

// Rules 返回 sidecar 需要的 iptables 规则命令。
func Rules(proxyPort, proxyUID int) [][]string {
	return [][]string{
		// 新建自定义链
		{"iptables", "-t", "nat", "-N", "LANE_SIDECAR_OUTPUT"},

		// sidecar 自身流量不拦截（避免死循环）
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-m", "owner", "--uid-owner", fmt.Sprint(proxyUID), "-j", "RETURN"},

		// localhost 不拦截
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-d", "127.0.0.1/32", "-j", "RETURN"},

		// 其余出站 TCP 重定向到 sidecar
		{"iptables", "-t", "nat", "-A", "LANE_SIDECAR_OUTPUT",
			"-p", "tcp", "-j", "REDIRECT", "--to-port", fmt.Sprint(proxyPort)},

		// 挂到 OUTPUT 链
		{"iptables", "-t", "nat", "-A", "OUTPUT", "-j", "LANE_SIDECAR_OUTPUT"},
	}
}

// Setup 执行 iptables 规则设置。
func Setup(proxyPort, proxyUID int) error {
	for _, args := range Rules(proxyPort, proxyUID) {
		cmd := exec.Command(args[0], args[1:]...)
		out, err := cmd.CombinedOutput()
		if err != nil {
			return fmt.Errorf("failed to run %s: %w\noutput: %s",
				strings.Join(args, " "), err, string(out))
		}
	}
	return nil
}
```

- [ ] **Step 2: 实现 main 入口**

```go
// apps/lane-sidecar/cmd/lane-sidecar/main.go
package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/chiwei-platform/lane-sidecar/internal/iptables"
	"github.com/chiwei-platform/lane-sidecar/internal/proxy"
	"github.com/chiwei-platform/lane-sidecar/internal/registry"
)

func main() {
	initMode := flag.Bool("init", false, "run iptables setup and exit (init container mode)")
	proxyPort := flag.Int("port", 15001, "proxy listen port")
	healthPort := flag.Int("health-port", 15021, "health check port")
	registryURL := flag.String("registry-url", envOrDefault("REGISTRY_URL", "http://lite-registry:8080"), "lite-registry URL")
	pollInterval := flag.Duration("poll-interval", 30*time.Second, "registry poll interval")
	flag.Parse()

	if *initMode {
		log.Println("[init] setting up iptables rules")
		if err := iptables.Setup(*proxyPort, iptables.ProxyUID); err != nil {
			log.Fatalf("[init] iptables setup failed: %v", err)
		}
		log.Println("[init] iptables rules applied successfully")
		return
	}

	// Proxy 模式
	reg := registry.NewClient(*registryURL, *pollInterval)
	defer reg.Stop()

	srv := proxy.NewServer(fmt.Sprintf(":%d", *proxyPort), reg)

	// 健康检查
	healthSrv := &http.Server{
		Addr: fmt.Sprintf(":%d", *healthPort),
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("ok"))
		}),
	}
	go healthSrv.ListenAndServe()

	// 优雅关闭
	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
		<-sigCh
		log.Println("[proxy] shutting down...")
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		srv.Shutdown(ctx)
		healthSrv.Shutdown(ctx)
	}()

	log.Printf("[proxy] starting on :%d, health on :%d", *proxyPort, *healthPort)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatalf("[proxy] server error: %v", err)
	}
}

func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
```

- [ ] **Step 3: 确认编译通过**

```bash
cd apps/lane-sidecar && go build ./cmd/lane-sidecar/
```
Expected: 无错误（在 Linux 上编译）

- [ ] **Step 4: 写 Dockerfile**

```dockerfile
# apps/lane-sidecar/Dockerfile
FROM harbor.local:30002/library/golang:1.25-alpine AS builder
WORKDIR /app
ENV GOPROXY=https://goproxy.cn,direct
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -ldflags="-s -w" -o /bin/lane-sidecar ./cmd/lane-sidecar

FROM harbor.local:30002/library/alpine:3.21
RUN apk add --no-cache ca-certificates iptables
COPY --from=builder /bin/lane-sidecar /usr/local/bin/lane-sidecar
# 非 root 运行代理模式（UID 1337）
# init 模式需要 root（设置 iptables）
ENTRYPOINT ["lane-sidecar"]
```

- [ ] **Step 5: 写 Makefile**

```makefile
# apps/lane-sidecar/Makefile
.PHONY: build test lint

build:
	go build -o output/lane-sidecar ./cmd/lane-sidecar

test:
	go test ./... -v -count=1

lint:
	go vet ./...
```

- [ ] **Step 6: 运行全量测试**

```bash
cd apps/lane-sidecar && make test
```
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add apps/lane-sidecar/
git commit -m "feat(lane-sidecar): add iptables init mode, main entry, and Dockerfile"
```

---

## Task 4: PaaS Engine — sidecar 注入

**Files:**
- Modify: `apps/paas-engine/internal/domain/app.go`
- Modify: `apps/paas-engine/internal/adapter/repository/model.go`
- Modify: `apps/paas-engine/internal/adapter/repository/app_repo.go`
- Modify: `apps/paas-engine/internal/service/app_service.go`
- Modify: `apps/paas-engine/internal/adapter/kubernetes/deployer.go`

- [ ] **Step 1: 域模型加 SidecarEnabled 字段**

在 `apps/paas-engine/internal/domain/app.go` 的 `App` struct 中添加：

```go
SidecarEnabled bool `json:"sidecar_enabled,omitempty"`
```

- [ ] **Step 2: 数据库模型加字段**

在 `apps/paas-engine/internal/adapter/repository/model.go` 的 `AppModel` struct 中添加：

```go
SidecarEnabled bool
```

- [ ] **Step 3: 更新 app_repo.go 的 toDomain/toModel 映射**

在 `toDomain` 方法中添加：
```go
app.SidecarEnabled = m.SidecarEnabled
```

在 `toModel` 方法中添加：
```go
model.SidecarEnabled = app.SidecarEnabled
```

- [ ] **Step 4: 更新 app_service.go 的 UpdateApp**

在 `UpdateApp` 方法的字段映射区域添加：
```go
if err := ApplyField(fields, "sidecar_enabled", &app.SidecarEnabled); err != nil {
    return nil, domain.ErrInvalidInput
}
```

- [ ] **Step 5: 数据库加列**

通过 `/ops-db` 执行：
```sql
ALTER TABLE apps ADD COLUMN IF NOT EXISTS sidecar_enabled BOOLEAN NOT NULL DEFAULT false;
```

- [ ] **Step 6: 写 deployer sidecar 注入测试**

在 `apps/paas-engine/internal/adapter/kubernetes/deployer_test.go` 中添加测试（如果文件不存在则创建）。测试 `applyDeployment` 生成的 Deployment spec 在 `app.SidecarEnabled=true` 时包含 sidecar container 和 init container：

```go
func TestApplyDeployment_WithSidecar(t *testing.T) {
    // 使用现有的 fake k8s client 模式
    // 构造 app.SidecarEnabled = true 的场景
    // 验证生成的 Deployment 有 2 个 containers + 1 个 initContainer
    // 验证 sidecar container: name=lane-sidecar, port=15001, runAsUser=1337
    // 验证 init container: name=lane-sidecar-init, command 包含 --init
}
```

具体测试代码需参照项目现有测试模式编写。

- [ ] **Step 7: 实现 deployer sidecar 注入**

在 `apps/paas-engine/internal/adapter/kubernetes/deployer.go` 的 `applyDeployment` 方法中，在构造 `deploy` 变量之前（约第 233 行），加入 sidecar 注入逻辑：

```go
// Sidecar injection
var initContainers []corev1.Container
var sidecarContainers []corev1.Container

if app.SidecarEnabled {
    sidecarImage := "harbor.local:30002/chiwei/lane-sidecar:latest"
    proxyUID := int64(1337)

    // Init container: 设置 iptables 规则
    initContainers = append(initContainers, corev1.Container{
        Name:    "lane-sidecar-init",
        Image:   sidecarImage,
        Command: []string{"lane-sidecar", "--init"},
        SecurityContext: &corev1.SecurityContext{
            Capabilities: &corev1.Capabilities{
                Add: []corev1.Capability{"NET_ADMIN"},
            },
            RunAsUser: func() *int64 { uid := int64(0); return &uid }(),
        },
    })

    // Sidecar container: 透明代理
    sidecarContainers = append(sidecarContainers, corev1.Container{
        Name:  "lane-sidecar",
        Image: sidecarImage,
        Env: []corev1.EnvVar{
            {Name: "REGISTRY_URL", Value: "http://lite-registry:8080"},
            {Name: "LANE", Value: release.Lane},
        },
        Ports: []corev1.ContainerPort{
            {Name: "sidecar", ContainerPort: 15001},
            {Name: "sidecar-health", ContainerPort: 15021},
        },
        LivenessProbe: &corev1.Probe{
            ProbeHandler: corev1.ProbeHandler{
                HTTPGet: &corev1.HTTPGetAction{
                    Path: "/healthz",
                    Port: intstr.FromInt(15021),
                },
            },
            InitialDelaySeconds: 2,
            PeriodSeconds:       10,
        },
        SecurityContext: &corev1.SecurityContext{
            RunAsUser: &proxyUID,
        },
    })
}
```

然后在 Deployment spec 中注入：

```go
Spec: corev1.PodSpec{
    ServiceAccountName: app.ServiceAccount,
    NodeSelector:       map[string]string{"node-role": "app"},
    InitContainers:     initContainers,
    Containers:         append([]corev1.Container{container}, sidecarContainers...),
},
```

需要在文件顶部添加 import：
```go
"k8s.io/apimachinery/pkg/util/intstr"
```

- [ ] **Step 8: 运行测试**

```bash
cd apps/paas-engine && make test
```
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add apps/paas-engine/
git commit -m "feat(paas-engine): support sidecar injection in deployment spec"
```

---

## Task 5: TS — 通用上下文传播中间件

**Files:**
- Create: `packages/ts-shared/src/middleware/context-propagation.ts`
- Create: `packages/ts-shared/src/middleware/context-propagation.test.ts`
- Modify: `packages/ts-shared/src/middleware/index.ts`

- [ ] **Step 1: 写测试**

```typescript
// packages/ts-shared/src/middleware/context-propagation.test.ts
import { describe, test, expect } from 'bun:test';
import { Hono } from 'hono';
import { createContextPropagationMiddleware, getContextHeaders } from './context-propagation';
import { asyncLocalStorage } from './context';

describe('contextPropagationMiddleware', () => {
    const middleware = createContextPropagationMiddleware();

    test('extracts x-ctx-* headers into AsyncLocalStorage', async () => {
        const app = new Hono();
        app.use('*', middleware);
        app.get('/test', (c) => {
            const store = asyncLocalStorage.getStore();
            return c.json({
                lane: store?.['ctx:lane'],
                gray: store?.['ctx:gray-group'],
            });
        });

        const res = await app.request('/test', {
            headers: {
                'x-ctx-lane': 'feat-test',
                'x-ctx-gray-group': 'beta',
                'x-unrelated': 'ignored',
            },
        });

        const body = await res.json();
        expect(body.lane).toBe('feat-test');
        expect(body.gray).toBe('beta');
    });

    test('getContextHeaders returns all x-ctx-* values from store', async () => {
        const app = new Hono();
        app.use('*', middleware);
        app.get('/test', (c) => {
            const headers = getContextHeaders();
            return c.json(headers);
        });

        const res = await app.request('/test', {
            headers: {
                'x-ctx-lane': 'dev',
                'x-ctx-trace-id': 'abc-123',
            },
        });

        const body = await res.json();
        expect(body['x-ctx-lane']).toBe('dev');
        expect(body['x-ctx-trace-id']).toBe('abc-123');
    });

    test('works with no x-ctx-* headers', async () => {
        const app = new Hono();
        app.use('*', middleware);
        app.get('/test', (c) => {
            return c.json(getContextHeaders());
        });

        const res = await app.request('/test');
        const body = await res.json();
        expect(Object.keys(body).length).toBe(0);
    });
});
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd packages/ts-shared && bun test src/middleware/context-propagation.test.ts
```
Expected: 模块不存在错误。

- [ ] **Step 3: 实现中间件**

```typescript
// packages/ts-shared/src/middleware/context-propagation.ts
import type { Context, Next } from 'hono';
import { asyncLocalStorage, type BaseRequestContext } from './context';

const CTX_PREFIX = 'x-ctx-';
const STORE_PREFIX = 'ctx:';

/**
 * Create a Hono middleware that propagates x-ctx-* headers via AsyncLocalStorage.
 *
 * Inbound: extracts all x-ctx-* headers, stores as ctx:* keys in AsyncLocalStorage.
 * Outbound: use getContextHeaders() to read them back for injection into outbound requests.
 */
export function createContextPropagationMiddleware() {
    return async (c: Context, next: Next) => {
        // 收集 x-ctx-* headers
        const ctxFields: Record<string, unknown> = {};
        for (const [key, value] of Object.entries(c.req.header())) {
            if (key.startsWith(CTX_PREFIX)) {
                const fieldName = STORE_PREFIX + key.slice(CTX_PREFIX.length);
                ctxFields[fieldName] = value;
            }
        }

        // 合并到当前 context（如果已有 trace middleware 创建的 store）
        const existing = asyncLocalStorage.getStore();
        if (existing) {
            Object.assign(existing, ctxFields);
            await next();
        } else {
            // 独立使用（没有 trace middleware 在外层）
            const store: BaseRequestContext = { traceId: '', ...ctxFields };
            await asyncLocalStorage.run(store, () => next());
        }
    };
}

/**
 * Get all x-ctx-* values from the current AsyncLocalStorage context.
 * Returns a header map ready to attach to outbound HTTP requests.
 */
export function getContextHeaders(): Record<string, string> {
    const store = asyncLocalStorage.getStore();
    if (!store) return {};

    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries(store)) {
        if (key.startsWith(STORE_PREFIX) && value != null) {
            const headerName = CTX_PREFIX + key.slice(STORE_PREFIX.length);
            headers[headerName] = String(value);
        }
    }
    return headers;
}
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
cd packages/ts-shared && bun test src/middleware/context-propagation.test.ts
```
Expected: PASS

- [ ] **Step 5: 更新 index.ts 导出**

在 `packages/ts-shared/src/middleware/index.ts` 末尾添加：

```typescript
// context-propagation
export { createContextPropagationMiddleware, getContextHeaders } from './context-propagation';
```

- [ ] **Step 6: Commit**

```bash
git add packages/ts-shared/
git commit -m "feat(ts-shared): add x-ctx-* context propagation middleware for Hono"
```

---

## Task 6: Python — 通用上下文传播中间件

**Files:**
- Create: `packages/py-shared/inner_shared/middlewares/context_propagation.py`
- Create: `packages/py-shared/tests/test_context_propagation.py`
- Modify: `packages/py-shared/inner_shared/middlewares/__init__.py`

- [ ] **Step 1: 写测试**

```python
# packages/py-shared/tests/test_context_propagation.py
import pytest
from inner_shared.middlewares.context_propagation import (
    ctx_vars,
    get_context_headers,
    init_ctx_vars,
)

# 测试需要 fastapi + httpx
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def app():
    """Create a FastAPI app with context propagation middleware."""
    from inner_shared.middlewares.context_propagation import (
        create_context_propagation_middleware,
    )

    app = FastAPI()
    app.add_middleware(create_context_propagation_middleware())

    @app.get("/test")
    async def test_endpoint():
        return get_context_headers()

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_extracts_ctx_headers(client):
    resp = client.get(
        "/test",
        headers={
            "x-ctx-lane": "feat-test",
            "x-ctx-gray-group": "beta",
            "x-unrelated": "ignored",
        },
    )
    body = resp.json()
    assert body["x-ctx-lane"] == "feat-test"
    assert body["x-ctx-gray-group"] == "beta"
    assert "x-unrelated" not in body


def test_no_ctx_headers(client):
    resp = client.get("/test")
    body = resp.json()
    assert body == {}


def test_ctx_headers_in_response(client):
    resp = client.get("/test", headers={"x-ctx-lane": "dev"})
    assert resp.headers.get("x-ctx-lane") == "dev"
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd packages/py-shared && uv run pytest tests/test_context_propagation.py -v
```
Expected: ImportError

- [ ] **Step 3: 实现中间件**

```python
# packages/py-shared/inner_shared/middlewares/context_propagation.py
"""
Generic x-ctx-* context propagation middleware for FastAPI.
Captures all x-ctx-* headers from inbound requests and stores them in contextvars.
Provides get_context_headers() for outbound request injection.
"""

import contextvars
from collections.abc import Callable
from typing import Any

CTX_PREFIX = "x-ctx-"

# Dynamic context variable storage for x-ctx-* headers
ctx_vars: dict[str, contextvars.ContextVar[str | None]] = {}


def init_ctx_vars():
    """Reset ctx_vars (mainly for testing)."""
    global ctx_vars
    ctx_vars = {}


def _get_or_create_var(name: str) -> contextvars.ContextVar[str | None]:
    """Get or create a contextvar for a given x-ctx-* header."""
    if name not in ctx_vars:
        ctx_vars[name] = contextvars.ContextVar(f"ctx_{name}", default=None)
    return ctx_vars[name]


def get_context_headers() -> dict[str, str]:
    """
    Read all x-ctx-* values from contextvars.
    Returns a dict ready to attach to outbound HTTP requests.
    """
    headers: dict[str, str] = {}
    for header_name, var in ctx_vars.items():
        value = var.get()
        if value is not None:
            headers[header_name] = value
    return headers


def create_context_propagation_middleware():
    """
    Create a FastAPI middleware that propagates x-ctx-* headers.
    """
    try:
        from starlette.middleware.base import BaseHTTPMiddleware
        from fastapi import Request, Response
    except ImportError:
        raise ImportError("FastAPI/Starlette required for context propagation middleware")

    class ContextPropagationMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Callable) -> Response:
            # Extract all x-ctx-* headers
            for key, value in request.headers.items():
                if key.lower().startswith(CTX_PREFIX):
                    var = _get_or_create_var(key.lower())
                    var.set(value)

            response = await call_next(request)

            # Echo x-ctx-* headers in response
            for header_name, var in ctx_vars.items():
                value = var.get()
                if value is not None:
                    response.headers[header_name] = value

            return response

    return ContextPropagationMiddleware
```

- [ ] **Step 4: 运行测试，确认通过**

```bash
cd packages/py-shared && uv run pytest tests/test_context_propagation.py -v
```
Expected: PASS

- [ ] **Step 5: 更新 __init__.py 导出**

在 `packages/py-shared/inner_shared/middlewares/__init__.py` 中添加（如果文件存在）或创建：

```python
from .context_propagation import (
    create_context_propagation_middleware,
    get_context_headers,
)
```

- [ ] **Step 6: Commit**

```bash
git add packages/py-shared/
git commit -m "feat(py-shared): add x-ctx-* context propagation middleware for FastAPI"
```

---

## Task 7: Go (paas-engine) — 上下文传播 middleware

**Files:**
- Modify: `apps/paas-engine/internal/adapter/http/middleware.go`
- Modify: `apps/paas-engine/internal/adapter/http/router.go`

- [ ] **Step 1: 读取现有 middleware.go 确认结构**

```bash
cat apps/paas-engine/internal/adapter/http/middleware.go
```

确认现有 middleware 的模式（chi middleware 签名）。

- [ ] **Step 2: 写上下文传播 middleware 测试**

在 `apps/paas-engine/internal/adapter/http/` 下新建或追加测试：

```go
// apps/paas-engine/internal/adapter/http/middleware_test.go
package http

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestContextPropagationMiddleware(t *testing.T) {
	// 模拟 handler 读取 context 中的 x-ctx-* 值
	handler := contextPropagationMiddleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		headers := GetContextHeaders(r.Context())
		if headers["x-ctx-lane"] != "dev" {
			t.Errorf("expected x-ctx-lane=dev, got %q", headers["x-ctx-lane"])
		}
		if _, ok := headers["x-unrelated"]; ok {
			t.Error("unexpected non-ctx header in context")
		}
		w.WriteHeader(http.StatusOK)
	}))

	req := httptest.NewRequest("GET", "/test", nil)
	req.Header.Set("x-ctx-lane", "dev")
	req.Header.Set("x-ctx-trace-id", "abc")
	req.Header.Set("x-unrelated", "ignored")
	w := httptest.NewRecorder()

	handler.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}
}
```

- [ ] **Step 3: 运行测试，确认失败**

```bash
cd apps/paas-engine && go test ./internal/adapter/http/ -run TestContextPropagation -v
```
Expected: 编译失败

- [ ] **Step 4: 实现 middleware**

在 `apps/paas-engine/internal/adapter/http/middleware.go` 中添加：

```go
// context key for x-ctx-* headers
type ctxHeadersKey struct{}

// contextPropagationMiddleware 提取入站请求的 x-ctx-* header 存入 context。
func contextPropagationMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		headers := make(map[string]string)
		for key, values := range r.Header {
			lk := strings.ToLower(key)
			if strings.HasPrefix(lk, "x-ctx-") && len(values) > 0 {
				headers[lk] = values[0]
			}
		}
		ctx := context.WithValue(r.Context(), ctxHeadersKey{}, headers)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// GetContextHeaders 从 context 中读取 x-ctx-* headers。
// 用于注入到出站 HTTP 请求中。
func GetContextHeaders(ctx context.Context) map[string]string {
	headers, _ := ctx.Value(ctxHeadersKey{}).(map[string]string)
	if headers == nil {
		return make(map[string]string)
	}
	return headers
}
```

确保文件顶部有 `"context"` 和 `"strings"` 的 import。

- [ ] **Step 5: 在 router.go 中挂载 middleware**

在 `apps/paas-engine/internal/adapter/http/router.go` 的 `NewRouter` 函数中，在现有 middleware 之后添加：

```go
r.Use(contextPropagationMiddleware)
```

加在 `loggingMiddleware` 之后、`bodySizeLimitMiddleware` 之前。

- [ ] **Step 6: 运行测试**

```bash
cd apps/paas-engine && make test
```
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add apps/paas-engine/
git commit -m "feat(paas-engine): add x-ctx-* context propagation middleware"
```

---

## Dependencies

```
Task 1 (registry client) ←── Task 2 (proxy server) ←── Task 3 (main + Dockerfile)
Task 4 (PaaS Engine sidecar injection) — 独立于 Task 1-3
Task 5 (TS middleware) — 完全独立
Task 6 (Python middleware) — 完全独立
Task 7 (Go middleware) — 完全独立
```

**可并行执行：**
- Task 1→2→3 串行（sidecar 构建链）
- Task 4, 5, 6, 7 各自独立，可与 Task 1-3 并行
