package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
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
		excludes := collectExcludeCIDRs()
		log.Println("[init] setting up iptables rules")
		if len(excludes) > 0 {
			log.Printf("[init] excluding CIDRs: %v", excludes)
		}
		if err := iptables.Setup(*proxyPort, iptables.ProxyUID, excludes); err != nil {
			log.Fatalf("[init] iptables setup failed: %v", err)
		}
		log.Println("[init] iptables rules applied successfully")
		return
	}

	reg := registry.NewClient(*registryURL, *pollInterval)
	defer reg.Stop()

	srv := proxy.NewServer(fmt.Sprintf(":%d", *proxyPort), reg)

	healthSrv := &http.Server{
		Addr: fmt.Sprintf(":%d", *healthPort),
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("ok"))
		}),
	}
	go healthSrv.ListenAndServe()

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

// collectExcludeCIDRs gathers IPs/CIDRs that should bypass sidecar interception.
// Sources: HTTPS_PROXY/HTTP_PROXY env vars (auto-detect proxy host) + SIDECAR_EXCLUDE_CIDRS (explicit).
func collectExcludeCIDRs() []string {
	var cidrs []string

	// 1. 从 HTTPS_PROXY / HTTP_PROXY 自动提取 proxy host IP
	for _, envKey := range []string{"HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"} {
		if proxyURL := os.Getenv(envKey); proxyURL != "" {
			if ips := resolveProxyHost(proxyURL); len(ips) > 0 {
				cidrs = append(cidrs, ips...)
				log.Printf("[init] auto-excluded proxy IPs from %s=%s: %v", envKey, proxyURL, ips)
				break // 只取第一个有值的
			}
		}
	}

	// 2. 显式排除列表
	if explicit := os.Getenv("SIDECAR_EXCLUDE_CIDRS"); explicit != "" {
		for _, cidr := range strings.Split(explicit, ",") {
			if c := strings.TrimSpace(cidr); c != "" {
				cidrs = append(cidrs, c)
			}
		}
	}

	return cidrs
}

// resolveProxyHost parses a proxy URL and resolves the hostname to IPs.
func resolveProxyHost(rawURL string) []string {
	u, err := url.Parse(rawURL)
	if err != nil {
		log.Printf("[init] failed to parse proxy URL %q: %v", rawURL, err)
		return nil
	}

	host := u.Hostname()
	if host == "" {
		return nil
	}

	// 已经是 IP 的直接返回
	if ip := net.ParseIP(host); ip != nil {
		return []string{ip.String()}
	}

	// DNS 解析
	ips, err := net.LookupHost(host)
	if err != nil {
		log.Printf("[init] failed to resolve proxy host %q: %v", host, err)
		return nil
	}
	return ips
}

func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
