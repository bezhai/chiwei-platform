package loki

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/port"
)

func TestQueryBuildLogs_Success(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/loki/api/v1/query_range" {
			t.Errorf("unexpected path: %s", r.URL.Path)
		}
		q := r.URL.Query().Get("query")
		if q == "" {
			t.Error("missing query parameter")
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{
			"status": "success",
			"data": {
				"resultType": "streams",
				"result": [
					{
						"stream": {},
						"values": [
							["1700000000000000000", "line1"],
							["1700000002000000000", "line3"]
						]
					},
					{
						"stream": {},
						"values": [
							["1700000001000000000", "line2"]
						]
					}
				]
			}
		}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	logs, err := c.QueryBuildLogs(context.Background(), "paas-builds", "abc-def-123", time.Unix(0, 0), time.Now())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	expected := "line1\nline2\nline3\n"
	if logs != expected {
		t.Errorf("got %q, want %q", logs, expected)
	}
}

func TestQueryBuildLogs_NonOKStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	_, err := c.QueryBuildLogs(context.Background(), "paas-builds", "abc", time.Unix(0, 0), time.Now())
	if err == nil {
		t.Fatal("expected error for non-OK status")
	}
}

func TestQueryBuildLogs_EmptyResult(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"status":"success","data":{"resultType":"streams","result":[]}}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	logs, err := c.QueryBuildLogs(context.Background(), "paas-builds", "abc", time.Unix(0, 0), time.Now())
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if logs != "" {
		t.Errorf("expected empty logs, got %q", logs)
	}
}

func TestQueryAppLogs_WithLane(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("query")
		if q == "" {
			t.Error("missing query parameter")
		}
		if !contains(q, `lane="prod"`) {
			t.Errorf("expected lane label in query, got %q", q)
		}
		if !contains(q, `app="myapp"`) {
			t.Errorf("expected app label in query, got %q", q)
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{
			"status": "success",
			"data": {
				"resultType": "streams",
				"result": [
					{
						"stream": {},
						"values": [
							["1700000000000000000", "runtime log line"]
						]
					}
				]
			}
		}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	logs, err := c.QueryAppLogs(context.Background(), port.AppLogQuery{
		Namespace: "prod",
		Apps:      []string{"myapp"},
		Lane:      "prod",
		Start:     time.Unix(0, 0),
		End:       time.Now(),
		Limit:     100,
		Direction: "forward",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if logs != "runtime log line\n" {
		t.Errorf("got %q, want %q", logs, "runtime log line\n")
	}
}

func TestQueryAppLogs_NoLane(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("query")
		if contains(q, "lane=") {
			t.Errorf("unexpected lane label in query: %q", q)
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"success","data":{"resultType":"streams","result":[]}}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	logs, err := c.QueryAppLogs(context.Background(), port.AppLogQuery{
		Namespace: "prod",
		Apps:      []string{"myapp"},
		Start:     time.Unix(0, 0),
		End:       time.Now(),
		Limit:     500,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if logs != "" {
		t.Errorf("expected empty logs, got %q", logs)
	}
}

func TestQueryAppLogs_NonOKStatus(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	_, err := c.QueryAppLogs(context.Background(), port.AppLogQuery{
		Namespace: "prod",
		Apps:      []string{"myapp"},
		Lane:      "prod",
		Start:     time.Unix(0, 0),
		End:       time.Now(),
		Limit:     100,
	})
	if err == nil {
		t.Fatal("expected error for non-OK status")
	}
}

func TestQueryAppLogs_WithKeywordAndExclude(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("query")
		if !contains(q, `|= "error"`) {
			t.Errorf("expected keyword filter, got %q", q)
		}
		if !contains(q, `!= "healthcheck"`) {
			t.Errorf("expected exclude filter, got %q", q)
		}
		w.Write([]byte(`{"status":"success","data":{"resultType":"streams","result":[]}}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	_, err := c.QueryAppLogs(context.Background(), port.AppLogQuery{
		Namespace: "prod",
		Apps:      []string{"agent-service"},
		Keyword:   "error",
		Exclude:   "healthcheck",
		Start:     time.Unix(0, 0),
		End:       time.Now(),
		Limit:     100,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestQueryAppLogs_WithRegexp(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("query")
		if !contains(q, `|~ "timeout|deadline"`) {
			t.Errorf("expected regexp filter, got %q", q)
		}
		w.Write([]byte(`{"status":"success","data":{"resultType":"streams","result":[]}}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	_, err := c.QueryAppLogs(context.Background(), port.AppLogQuery{
		Namespace: "prod",
		Apps:      []string{"lark-server"},
		Regexp:    "timeout|deadline",
		Start:     time.Unix(0, 0),
		End:       time.Now(),
		Limit:     100,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestQueryAppLogs_NoApp(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("query")
		if contains(q, "app=") {
			t.Errorf("unexpected app label in query: %q", q)
		}
		if !contains(q, `namespace="prod"`) {
			t.Errorf("expected namespace in query, got %q", q)
		}
		w.Write([]byte(`{"status":"success","data":{"resultType":"streams","result":[]}}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	_, err := c.QueryAppLogs(context.Background(), port.AppLogQuery{
		Namespace: "prod",
		Keyword:   "error",
		Start:     time.Unix(0, 0),
		End:       time.Now(),
		Limit:     1000,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestQueryAppLogs_MultiApp(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("query")
		if !contains(q, `app=~"lark-server|agent-service"`) {
			t.Errorf("expected multi-app regex, got %q", q)
		}
		w.Write([]byte(`{"status":"success","data":{"resultType":"streams","result":[]}}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	_, err := c.QueryAppLogs(context.Background(), port.AppLogQuery{
		Namespace: "prod",
		Apps:      []string{"lark-server", "agent-service"},
		Start:     time.Unix(0, 0),
		End:       time.Now(),
		Limit:     100,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestQueryAppLogs_PodPrefix(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("query")
		if !contains(q, `pod=~"lark-server-abc.*"`) {
			t.Errorf("expected pod prefix match, got %q", q)
		}
		w.Write([]byte(`{"status":"success","data":{"resultType":"streams","result":[]}}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	_, err := c.QueryAppLogs(context.Background(), port.AppLogQuery{
		Namespace: "prod",
		Apps:      []string{"lark-server"},
		Pod:       "lark-server-abc",
		Start:     time.Unix(0, 0),
		End:       time.Now(),
		Limit:     100,
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
}

func TestQueryAppLogs_BackwardDirection(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		dir := r.URL.Query().Get("direction")
		if dir != "backward" {
			t.Errorf("expected backward direction, got %q", dir)
		}
		w.Write([]byte(`{
			"status": "success",
			"data": {
				"resultType": "streams",
				"result": [
					{
						"stream": {},
						"values": [
							["1700000000000000000", "older"],
							["1700000002000000000", "newer"]
						]
					}
				]
			}
		}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	logs, err := c.QueryAppLogs(context.Background(), port.AppLogQuery{
		Namespace: "prod",
		Apps:      []string{"myapp"},
		Start:     time.Unix(0, 0),
		End:       time.Now(),
		Limit:     100,
		Direction: "backward",
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	// backward: newer first
	expected := "newer\nolder\n"
	if logs != expected {
		t.Errorf("got %q, want %q", logs, expected)
	}
}

func TestBuildLogQL(t *testing.T) {
	tests := []struct {
		name     string
		query    port.AppLogQuery
		expected string
	}{
		{
			name:     "namespace only",
			query:    port.AppLogQuery{Namespace: "prod"},
			expected: `{namespace="prod"}`,
		},
		{
			name:     "single app",
			query:    port.AppLogQuery{Namespace: "prod", Apps: []string{"myapp"}},
			expected: `{namespace="prod", app="myapp"}`,
		},
		{
			name:     "multi app",
			query:    port.AppLogQuery{Namespace: "prod", Apps: []string{"a", "b", "c"}},
			expected: `{namespace="prod", app=~"a|b|c"}`,
		},
		{
			name:     "with lane",
			query:    port.AppLogQuery{Namespace: "prod", Apps: []string{"myapp"}, Lane: "dev"},
			expected: `{namespace="prod", app="myapp", lane="dev"}`,
		},
		{
			name:     "with pod prefix",
			query:    port.AppLogQuery{Namespace: "prod", Pod: "myapp-abc"},
			expected: `{namespace="prod", pod=~"myapp-abc.*"}`,
		},
		{
			name:     "with keyword",
			query:    port.AppLogQuery{Namespace: "prod", Keyword: "error"},
			expected: `{namespace="prod"} |= "error"`,
		},
		{
			name:     "with exclude",
			query:    port.AppLogQuery{Namespace: "prod", Exclude: "health"},
			expected: `{namespace="prod"} != "health"`,
		},
		{
			name:     "with regexp",
			query:    port.AppLogQuery{Namespace: "prod", Regexp: "timeout|deadline"},
			expected: `{namespace="prod"} |~ "timeout|deadline"`,
		},
		{
			name: "full combination",
			query: port.AppLogQuery{
				Namespace: "prod",
				Apps:      []string{"lark-server"},
				Lane:      "dev",
				Keyword:   "error",
				Exclude:   "healthcheck",
				Regexp:    "timeout|deadline",
			},
			expected: `{namespace="prod", app="lark-server", lane="dev"} |= "error" != "healthcheck" |~ "timeout|deadline"`,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := buildLogQL(tt.query)
			if got != tt.expected {
				t.Errorf("buildLogQL() = %q, want %q", got, tt.expected)
			}
		})
	}
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && (s == substr || len(s) > 0 && containsStr(s, substr))
}

func containsStr(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
