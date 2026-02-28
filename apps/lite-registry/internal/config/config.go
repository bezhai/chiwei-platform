package config

import (
	"os"
	"strconv"
)

type Config struct {
	HTTPPort       string
	KubeconfigPath string
	Namespace      string
	ResyncSeconds  int
}

func Load() Config {
	c := Config{
		HTTPPort:       "8080",
		KubeconfigPath: os.Getenv("KUBECONFIG"),
		Namespace:      "prod",
		ResyncSeconds:  60,
	}

	if v := os.Getenv("HTTP_PORT"); v != "" {
		c.HTTPPort = v
	}
	if v := os.Getenv("NAMESPACE"); v != "" {
		c.Namespace = v
	}
	if v := os.Getenv("RESYNC_SECONDS"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			c.ResyncSeconds = n
		}
	}

	return c
}
