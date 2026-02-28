package config

import (
	"os"
	"strconv"
)

type Config struct {
	HTTPPort            string
	RegistryURL         string
	RoutesConfig        string
	PollIntervalSeconds int
	ProxyTimeoutSeconds int
}

func Load() *Config {
	return &Config{
		HTTPPort:            getEnv("HTTP_PORT", "8080"),
		RegistryURL:         getEnv("REGISTRY_URL", "http://lite-registry:8080"),
		RoutesConfig:        getEnv("ROUTES_CONFIG", "/etc/api-gateway/routes.yaml"),
		PollIntervalSeconds: getEnvInt("POLL_INTERVAL_SECONDS", 30),
		ProxyTimeoutSeconds: getEnvInt("PROXY_TIMEOUT_SECONDS", 60),
	}
}

func getEnv(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}

func getEnvInt(key string, defaultVal int) int {
	v := os.Getenv(key)
	if v == "" {
		return defaultVal
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return defaultVal
	}
	return n
}
