package service

import (
	"encoding/json"
	"testing"
)

func TestApplyField_Present(t *testing.T) {
	fields := map[string]json.RawMessage{
		"port": json.RawMessage(`9090`),
	}
	var port int
	if err := ApplyField(fields, "port", &port); err != nil {
		t.Fatal(err)
	}
	if port != 9090 {
		t.Errorf("port = %d, want 9090", port)
	}
}

func TestApplyField_Absent(t *testing.T) {
	fields := map[string]json.RawMessage{}
	port := 8080
	if err := ApplyField(fields, "port", &port); err != nil {
		t.Fatal(err)
	}
	if port != 8080 {
		t.Errorf("port = %d, want 8080 (unchanged)", port)
	}
}

func TestApplyField_TypeError(t *testing.T) {
	fields := map[string]json.RawMessage{
		"port": json.RawMessage(`"not a number"`),
	}
	var port int
	if err := ApplyField(fields, "port", &port); err == nil {
		t.Error("expected error for type mismatch")
	}
}

func TestMergeEnvs_NotPresent(t *testing.T) {
	existing := map[string]string{"A": "1"}
	got, err := MergeEnvs(existing, nil)
	if err != nil {
		t.Fatal(err)
	}
	if got["A"] != "1" {
		t.Errorf("expected A=1, got %v", got)
	}
}

func TestMergeEnvs_Null(t *testing.T) {
	existing := map[string]string{"A": "1"}
	got, err := MergeEnvs(existing, json.RawMessage(`null`))
	if err != nil {
		t.Fatal(err)
	}
	if got != nil {
		t.Errorf("expected nil, got %v", got)
	}
}

func TestMergeEnvs_EmptyObject(t *testing.T) {
	existing := map[string]string{"A": "1"}
	got, err := MergeEnvs(existing, json.RawMessage(`{}`))
	if err != nil {
		t.Fatal(err)
	}
	if got["A"] != "1" {
		t.Errorf("expected A=1, got %v", got)
	}
}

func TestMergeEnvs_MergeKeys(t *testing.T) {
	existing := map[string]string{"A": "1", "B": "2"}
	got, err := MergeEnvs(existing, json.RawMessage(`{"C":"3"}`))
	if err != nil {
		t.Fatal(err)
	}
	if got["A"] != "1" || got["B"] != "2" || got["C"] != "3" {
		t.Errorf("unexpected result: %v", got)
	}
}

func TestMergeEnvs_DeleteKey(t *testing.T) {
	existing := map[string]string{"A": "1", "B": "2"}
	got, err := MergeEnvs(existing, json.RawMessage(`{"A":null}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := got["A"]; ok {
		t.Error("A should be deleted")
	}
	if got["B"] != "2" {
		t.Errorf("B should remain, got %v", got)
	}
}

func TestMergeEnvs_Complex(t *testing.T) {
	existing := map[string]string{"A": "1", "B": "2", "C": "3"}
	got, err := MergeEnvs(existing, json.RawMessage(`{"A":null,"B":"updated","D":"new"}`))
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := got["A"]; ok {
		t.Error("A should be deleted")
	}
	if got["B"] != "updated" {
		t.Errorf("B = %q, want updated", got["B"])
	}
	if got["C"] != "3" {
		t.Errorf("C = %q, want 3", got["C"])
	}
	if got["D"] != "new" {
		t.Errorf("D = %q, want new", got["D"])
	}
}
