package config

import (
	"os"
	"strings"
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
	APIToken        string
	LokiURL         string
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
		APIToken:        os.Getenv("API_TOKEN"),
		LokiURL:         getEnv("LOKI_URL", "http://loki-gateway.monitoring.svc.cluster.local"),
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

func getEnv(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}

