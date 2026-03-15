package config

import (
	"os"
	"strings"
	"time"
)

type Config struct {
	HTTPPort        string
	DatabaseURL     string
	KubeconfigPath  string
	DeployNamespace string
	KanikoNamespace string
	KanikoImage       string
	RegistrySecret    string
	RegistryMirrors   []string
	InsecureRegistries []string
	RegistryBase      string
	KanikoCacheRepo   string
	BuildHttpProxy    string
	BuildNoProxy      string
	APIToken          string
	LokiURL           string
	ChiweiDatabaseURL string

	// CI Pipeline
	CINamespace     string        // K8s namespace for CI test jobs
	CIGitRepo       string        // monorepo git URL for CI test jobs
	GitHubToken     string        // GitHub PAT for polling branch commits
	GitPollInterval time.Duration // git polling interval (default 60s)
}

func Load() *Config {
	return &Config{
		HTTPPort:        getEnv("HTTP_PORT", "8080"),
		DatabaseURL:     getEnv("DATABASE_URL", "postgres://paas:paas@localhost:5432/paas_engine?sslmode=disable"),
		KubeconfigPath:  getEnv("KUBECONFIG", ""),
		DeployNamespace: getEnv("DEPLOY_NAMESPACE", "default"),
		KanikoNamespace: getEnv("KANIKO_NAMESPACE", "paas-builds"),
		KanikoImage:        getEnv("KANIKO_IMAGE", "harbor.local:30002/inner-bot/kaniko:latest"),
		RegistrySecret:    getEnv("REGISTRY_SECRET", "harbor-secret"),
		RegistryMirrors:    splitCSV(os.Getenv("REGISTRY_MIRRORS")),
		InsecureRegistries: splitCSV(os.Getenv("INSECURE_REGISTRIES")),
		RegistryBase:      getEnv("REGISTRY_BASE", "registry.example.com"),
		KanikoCacheRepo:   os.Getenv("KANIKO_CACHE_REPO"),
		BuildHttpProxy:    os.Getenv("BUILD_HTTP_PROXY"),
		BuildNoProxy:      os.Getenv("BUILD_NO_PROXY"),
		APIToken:          os.Getenv("API_TOKEN"),
		LokiURL:           getEnv("LOKI_URL", "http://loki-gateway.monitoring.svc.cluster.local"),
		ChiweiDatabaseURL: os.Getenv("CHIWEI_DATABASE_URL"),

		CINamespace:     getEnv("CI_NAMESPACE", "paas-builds"),
		CIGitRepo:       os.Getenv("CI_GIT_REPO"),
		GitHubToken:     os.Getenv("GITHUB_TOKEN"),
		GitPollInterval: parseDuration(os.Getenv("GIT_POLL_INTERVAL"), 60*time.Second),
	}
}

func splitCSV(s string) []string {
	if s == "" {
		return nil
	}
	parts := strings.Split(s, ",")
	result := make([]string, 0, len(parts))
	for _, p := range parts {
		if v := strings.TrimSpace(p); v != "" {
			result = append(result, v)
		}
	}
	return result
}

func parseDuration(s string, defaultVal time.Duration) time.Duration {
	if s == "" {
		return defaultVal
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		return defaultVal
	}
	return d
}

func getEnv(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}

