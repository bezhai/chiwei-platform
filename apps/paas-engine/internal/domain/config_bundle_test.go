package domain

import "testing"

func TestConfigBundle_ClassOverridesField(t *testing.T) {
	b := ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"POSTGRES_HOST": "postgres"},
		ClassOverrides: map[string]map[string]string{
			"coe": {"POSTGRES_HOST": "chiwei-test-postgres"},
		},
	}
	got, ok := b.ClassOverrides["coe"]["POSTGRES_HOST"]
	if !ok || got != "chiwei-test-postgres" {
		t.Fatalf("ClassOverrides[coe][POSTGRES_HOST] = %q, want chiwei-test-postgres", got)
	}
}

func TestConfigBundle_RequiredKeysField(t *testing.T) {
	b := ConfigBundle{
		Name:         "pg-main",
		RequiredKeys: map[string][]string{"coe": {"POSTGRES_HOST", "POSTGRES_DB"}},
	}
	got := b.RequiredKeys["coe"]
	if len(got) != 2 {
		t.Fatalf("RequiredKeys[coe] len = %d, want 2", len(got))
	}
}
