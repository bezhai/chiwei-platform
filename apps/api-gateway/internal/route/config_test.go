package route

import (
	"testing"
)

func TestParse(t *testing.T) {
	yaml := `
routes:
  - prefix: /short/
    service: svc-a
    port: 80
  - prefix: /longer/path/
    service: svc-b
    port: 8080
    strip_prefix: /longer/path
    rewrite_prefix: /api
`
	routes, err := Parse([]byte(yaml))
	if err != nil {
		t.Fatalf("Parse error: %v", err)
	}
	if len(routes) != 2 {
		t.Fatalf("expected 2 routes, got %d", len(routes))
	}
	// Longest prefix should be first
	if routes[0].Prefix != "/longer/path/" {
		t.Errorf("expected longest prefix first, got %q", routes[0].Prefix)
	}
	if routes[0].StripPrefix != "/longer/path" {
		t.Errorf("expected strip_prefix, got %q", routes[0].StripPrefix)
	}
}

func TestParseEmpty(t *testing.T) {
	_, err := Parse([]byte("routes: []"))
	if err == nil {
		t.Error("expected error for empty routes")
	}
}

func TestParseInvalidYAML(t *testing.T) {
	_, err := Parse([]byte("not: valid: yaml: ["))
	if err == nil {
		t.Error("expected error for invalid YAML")
	}
}
