package loki

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
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
		// 验证 query 包含 lane label
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
	logs, err := c.QueryAppLogs(context.Background(), "prod", "myapp", "prod", time.Unix(0, 0), time.Now(), 100)
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
		// 无 lane 时不应包含 lane label
		if contains(q, "lane=") {
			t.Errorf("unexpected lane label in query: %q", q)
		}
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"status":"success","data":{"resultType":"streams","result":[]}}`))
	}))
	defer srv.Close()

	c := NewClient(srv.URL)
	logs, err := c.QueryAppLogs(context.Background(), "prod", "myapp", "", time.Unix(0, 0), time.Now(), 500)
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
	_, err := c.QueryAppLogs(context.Background(), "prod", "myapp", "prod", time.Unix(0, 0), time.Now(), 100)
	if err == nil {
		t.Fatal("expected error for non-OK status")
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
