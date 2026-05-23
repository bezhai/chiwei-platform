package loader

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

// onePayload is in the EXACT wire format paas-engine emits from
// domain.GatewaySnapshot: "version" is a bare JSON number (int64), not a
// quoted string, and "updated_at" is an RFC3339 timestamp (time.Time).
const onePayload = `{
  "version": 1,
  "updated_at": "2026-05-22T10:00:00Z",
  "rules": [
    {"name":"r1","enabled":true,"priority":100,
     "match":{"path_prefix":"/api/paas/"},
     "targets":[{"service":"paas-engine","port":8080,"weight":100}],
     "fallback":{"mode":"prod"}}
  ]
}`

func mockServer(t *testing.T, status int, body string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/internal/gateway-rules" {
			t.Errorf("unexpected path %q", r.URL.Path)
		}
		w.WriteHeader(status)
		w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestFetchAndSwapHappy(t *testing.T) {
	srv := mockServer(t, http.StatusOK, onePayload)
	l := New(srv.URL)

	if err := l.fetchOnce(); err != nil {
		t.Fatalf("fetchOnce: %v", err)
	}
	snap := l.Current()
	if snap == nil {
		t.Fatal("expected snapshot, got nil")
	}
	if len(snap.Rules()) != 1 {
		t.Fatalf("expected 1 rule, got %d", len(snap.Rules()))
	}
	if snap.Version() != 1 {
		t.Errorf("version: got %d want 1", snap.Version())
	}
}

// TestRealContractPayloadParses pins the wire contract with paas-engine:
// "version" is a bare JSON number. If api-gateway's payload struct ever drifts
// back to a string-typed Version, json.Decode fails here and this test catches
// the feature-breaking regression (number-into-string => decode error => current
// stays nil => business paths 503 forever).
func TestRealContractPayloadParses(t *testing.T) {
	// Mirrors domain.GatewaySnapshot serialization exactly.
	body := `{
	  "version": 42,
	  "updated_at": "2026-05-22T10:00:00Z",
	  "rules": [
	    {"name":"agent","enabled":true,"priority":100,
	     "path_prefix":"/api/agent/","request_lane":"",
	     "match":{"path_prefix":"/api/agent/","request_lane":""},
	     "targets":[{"service":"agent-service","lane":"","port":8000,"weight":100}],
	     "fallback":{"mode":"prod"},
	     "created_at":"2026-05-22T09:00:00Z","updated_at":"2026-05-22T10:00:00Z","version":42}
	  ]
	}`
	srv := mockServer(t, http.StatusOK, body)
	l := New(srv.URL)
	if err := l.fetchOnce(); err != nil {
		t.Fatalf("fetchOnce on real contract payload: %v", err)
	}
	snap := l.Current()
	if snap == nil {
		t.Fatal("expected snapshot from real contract payload, got nil")
	}
	if snap.Version() != 42 {
		t.Errorf("version: got %d want 42", snap.Version())
	}
	if len(snap.Rules()) != 1 {
		t.Fatalf("expected 1 rule, got %d", len(snap.Rules()))
	}
}

func TestColdStartCurrentNil(t *testing.T) {
	l := New("http://127.0.0.1:1") // unreachable
	if l.Current() != nil {
		t.Error("expected nil current before any successful fetch")
	}
}

func TestEmptyRulesKeepsLastGood(t *testing.T) {
	// First a good fetch.
	good := mockServer(t, http.StatusOK, onePayload)
	l := New(good.URL)
	if err := l.fetchOnce(); err != nil {
		t.Fatalf("first fetch: %v", err)
	}
	first := l.Current()
	if first == nil {
		t.Fatal("expected good snapshot")
	}

	// Now point at an empty-rules payload.
	empty := mockServer(t, http.StatusOK, `{"version":2,"rules":[]}`)
	l.url = empty.URL + "/internal/gateway-rules"
	if err := l.fetchOnce(); err == nil {
		t.Error("expected error for empty rules")
	}
	// current must be unchanged (last-good preserved)
	if l.Current() != first {
		t.Error("empty rules must keep last-good snapshot")
	}
}

// TestAllDisabledRulesKeepsLastGood: a non-empty payload whose rules are ALL
// enabled=false is semantically an empty snapshot (matcher skips disabled rules
// => every path 404s). It must be treated as a failure and keep last-good, not
// atomically swap in a snapshot that 404s everything.
func TestAllDisabledRulesKeepsLastGood(t *testing.T) {
	good := mockServer(t, http.StatusOK, onePayload)
	l := New(good.URL)
	if err := l.fetchOnce(); err != nil {
		t.Fatalf("first fetch: %v", err)
	}
	first := l.Current()
	if first == nil {
		t.Fatal("expected good snapshot")
	}

	allDisabled := mockServer(t, http.StatusOK, `{"version":3,"rules":[
	  {"name":"r1","enabled":false,"priority":100,
	   "match":{"path_prefix":"/api/paas/"},
	   "targets":[{"service":"paas-engine","port":8080,"weight":100}],
	   "fallback":{"mode":"prod"}}
	]}`)
	l.url = allDisabled.URL + "/internal/gateway-rules"
	if err := l.fetchOnce(); err == nil {
		t.Error("expected error for all-disabled rules")
	}
	if l.Current() != first {
		t.Error("all-disabled rules must keep last-good snapshot")
	}
}

func TestNetworkErrorKeepsLastGood(t *testing.T) {
	good := mockServer(t, http.StatusOK, onePayload)
	l := New(good.URL)
	if err := l.fetchOnce(); err != nil {
		t.Fatalf("first fetch: %v", err)
	}
	first := l.Current()

	l.url = "http://127.0.0.1:1/internal/gateway-rules" // unreachable
	if err := l.fetchOnce(); err == nil {
		t.Error("expected network error")
	}
	if l.Current() != first {
		t.Error("network error must keep last-good")
	}
}

func TestHTTP5xxKeepsLastGood(t *testing.T) {
	good := mockServer(t, http.StatusOK, onePayload)
	l := New(good.URL)
	l.fetchOnce()
	first := l.Current()

	bad := mockServer(t, http.StatusInternalServerError, "boom")
	l.url = bad.URL + "/internal/gateway-rules"
	if err := l.fetchOnce(); err == nil {
		t.Error("expected error on 5xx")
	}
	if l.Current() != first {
		t.Error("5xx must keep last-good")
	}
}

func TestInvalidJSONKeepsLastGood(t *testing.T) {
	good := mockServer(t, http.StatusOK, onePayload)
	l := New(good.URL)
	l.fetchOnce()
	first := l.Current()

	bad := mockServer(t, http.StatusOK, "{not json")
	l.url = bad.URL + "/internal/gateway-rules"
	if err := l.fetchOnce(); err == nil {
		t.Error("expected decode error")
	}
	if l.Current() != first {
		t.Error("invalid JSON must keep last-good")
	}
}

func TestValidationRejectsNilFields(t *testing.T) {
	cases := map[string]string{
		"empty name":          `{"version":1,"rules":[{"enabled":true,"match":{"path_prefix":"/x/"},"targets":[{"service":"s","port":80,"weight":100}],"fallback":{"mode":"prod"}}]}`,
		"empty path_prefix":   `{"version":1,"rules":[{"name":"r","enabled":true,"match":{"path_prefix":""},"targets":[{"service":"s","port":80,"weight":100}],"fallback":{"mode":"prod"}}]}`,
		"no targets":          `{"version":1,"rules":[{"name":"r","enabled":true,"match":{"path_prefix":"/x/"},"targets":[],"fallback":{"mode":"prod"}}]}`,
		"target empty service": `{"version":1,"rules":[{"name":"r","enabled":true,"match":{"path_prefix":"/x/"},"targets":[{"service":"","port":80,"weight":100}],"fallback":{"mode":"prod"}}]}`,
		"port zero":           `{"version":1,"rules":[{"name":"r","enabled":true,"match":{"path_prefix":"/x/"},"targets":[{"service":"s","port":0,"weight":100}],"fallback":{"mode":"prod"}}]}`,
		"port too high":       `{"version":1,"rules":[{"name":"r","enabled":true,"match":{"path_prefix":"/x/"},"targets":[{"service":"s","port":70000,"weight":100}],"fallback":{"mode":"prod"}}]}`,
	}
	for name, body := range cases {
		t.Run(name, func(t *testing.T) {
			srv := mockServer(t, http.StatusOK, body)
			l := New(srv.URL)
			if err := l.fetchOnce(); err == nil {
				t.Errorf("%s: expected validation error", name)
			}
			if l.Current() != nil {
				t.Errorf("%s: cold start must stay nil on invalid", name)
			}
		})
	}
}

func TestValidPayloadParsesAllFields(t *testing.T) {
	// The "fallback" field is still present in this payload on purpose: during
	// cutover paas-engine may keep emitting it. The loader must tolerate the
	// now-unknown field (json ignores it) and parse everything else.
	body := `{"version":9,"rules":[
	  {"name":"agent","enabled":true,"priority":100,
	   "match":{"path_prefix":"/api/agent/","request_lane":"ppe-x"},
	   "targets":[{"service":"agent-service","lane":"prod","port":8000,"weight":100,"strip_prefix":"/api/agent","rewrite_prefix":""}],
	   "fallback":{"mode":"reject"}}
	]}`
	srv := mockServer(t, http.StatusOK, body)
	l := New(srv.URL)
	if err := l.fetchOnce(); err != nil {
		t.Fatalf("fetchOnce: %v", err)
	}
	r := l.Current().Rules()[0]
	if r.Name != "agent" || r.Match.PathPrefix != "/api/agent/" || r.Match.RequestLane != "ppe-x" {
		t.Errorf("match parse wrong: %+v", r)
	}
	tg := r.Targets[0]
	if tg.Service != "agent-service" || tg.Lane != "prod" || tg.Port != 8000 || tg.StripPrefix != "/api/agent" || tg.Weight != 100 {
		t.Errorf("target parse wrong: %+v", tg)
	}
}
