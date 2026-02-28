package route

import (
	"fmt"
	"os"
	"sort"

	"gopkg.in/yaml.v3"
)

type Route struct {
	Prefix        string `yaml:"prefix"`
	Service       string `yaml:"service"`
	Port          int    `yaml:"port"`
	StripPrefix   string `yaml:"strip_prefix"`
	RewritePrefix string `yaml:"rewrite_prefix"`
}

type routesFile struct {
	Routes []Route `yaml:"routes"`
}

// LoadFromFile parses a YAML routes config file.
// Routes are sorted by prefix length descending for longest-prefix-first matching.
func LoadFromFile(path string) ([]Route, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read routes config: %w", err)
	}
	return Parse(data)
}

// Parse parses YAML bytes into sorted routes.
func Parse(data []byte) ([]Route, error) {
	var f routesFile
	if err := yaml.Unmarshal(data, &f); err != nil {
		return nil, fmt.Errorf("parse routes config: %w", err)
	}
	if len(f.Routes) == 0 {
		return nil, fmt.Errorf("routes config: no routes defined")
	}
	sortRoutes(f.Routes)
	return f.Routes, nil
}

func sortRoutes(routes []Route) {
	sort.Slice(routes, func(i, j int) bool {
		return len(routes[i].Prefix) > len(routes[j].Prefix)
	})
}
