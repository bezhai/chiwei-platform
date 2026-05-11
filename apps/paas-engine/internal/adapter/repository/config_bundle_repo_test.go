package repository

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// TestBundleToModel_RoundTrip tests serialization of ClassOverrides and RequiredKeys.
func TestBundleToModel_ClassOverridesAndRequiredKeys(t *testing.T) {
	bundle := &domain.ConfigBundle{
		Name:        "test-bundle",
		Description: "test",
		Keys:        map[string]string{"KEY": "value"},
		ClassOverrides: map[string]map[string]string{
			"coe": {"KEY": "override"},
		},
		RequiredKeys: map[string][]string{
			"coe": {"KEY"},
		},
		CreatedAt: time.Now(),
		UpdatedAt: time.Now(),
	}

	model, err := bundleToModel(bundle)
	if err != nil {
		t.Fatalf("bundleToModel failed: %v", err)
	}

	// Verify ClassOverrides JSON is valid
	if model.ClassOverrides == "" {
		t.Fatal("ClassOverrides should not be empty")
	}
	var classOverrides map[string]map[string]string
	if err := json.Unmarshal([]byte(model.ClassOverrides), &classOverrides); err != nil {
		t.Fatalf("failed to unmarshal ClassOverrides JSON: %v", err)
	}
	if classOverrides["coe"]["KEY"] != "override" {
		t.Fatalf("ClassOverrides not preserved: %+v", classOverrides)
	}

	// Verify RequiredKeys JSON is valid
	if model.RequiredKeys == "" {
		t.Fatal("RequiredKeys should not be empty")
	}
	var requiredKeys map[string][]string
	if err := json.Unmarshal([]byte(model.RequiredKeys), &requiredKeys); err != nil {
		t.Fatalf("failed to unmarshal RequiredKeys JSON: %v", err)
	}
	if len(requiredKeys["coe"]) != 1 || requiredKeys["coe"][0] != "KEY" {
		t.Fatalf("RequiredKeys not preserved: %+v", requiredKeys)
	}
}

// TestModelToBundle_ClassOverridesAndRequiredKeys tests deserialization.
func TestModelToBundle_ClassOverridesAndRequiredKeys(t *testing.T) {
	classOverridesJSON := `{"coe":{"KEY":"override"}}`
	requiredKeysJSON := `{"coe":["KEY"]}`

	model := &ConfigBundleModel{
		Name:           "test-bundle",
		Description:    "test",
		Keys:           `{"KEY":"value"}`,
		ClassOverrides: classOverridesJSON,
		RequiredKeys:   requiredKeysJSON,
		CreatedAt:      time.Now(),
		UpdatedAt:      time.Now(),
	}

	bundle, err := modelToBundle(model)
	if err != nil {
		t.Fatalf("modelToBundle failed: %v", err)
	}

	if bundle.ClassOverrides["coe"]["KEY"] != "override" {
		t.Fatalf("ClassOverrides not deserialized: %+v", bundle.ClassOverrides)
	}
	if len(bundle.RequiredKeys["coe"]) != 1 || bundle.RequiredKeys["coe"][0] != "KEY" {
		t.Fatalf("RequiredKeys not deserialized: %+v", bundle.RequiredKeys)
	}
}

// TestRoundTrip_ComplexClassOverridesAndRequiredKeys tests bundleToModel then modelToBundle.
func TestRoundTrip_ComplexClassOverridesAndRequiredKeys(t *testing.T) {
	original := &domain.ConfigBundle{
		Name:        "pg-main",
		Description: "Production PostgreSQL",
		Keys: map[string]string{
			"POSTGRES_HOST": "prod-postgres",
			"POSTGRES_PORT": "5432",
		},
		ClassOverrides: map[string]map[string]string{
			"coe": {
				"POSTGRES_HOST": "chiwei-test-postgres",
				"POSTGRES_DB":   "chiwei_test",
			},
			"private": {
				"POSTGRES_HOST": "private-postgres",
			},
		},
		RequiredKeys: map[string][]string{
			"coe":     {"POSTGRES_HOST", "POSTGRES_DB"},
			"private": {"POSTGRES_HOST"},
		},
		LaneOverrides: map[string]map[string]string{
			"dev": {"POSTGRES_PORT": "5433"},
		},
		CreatedAt: time.Now(),
		UpdatedAt: time.Now(),
	}

	// Serialize to model
	model, err := bundleToModel(original)
	if err != nil {
		t.Fatalf("bundleToModel failed: %v", err)
	}

	// Deserialize back to bundle
	deserialized, err := modelToBundle(model)
	if err != nil {
		t.Fatalf("modelToBundle failed: %v", err)
	}

	// Verify ClassOverrides
	if len(deserialized.ClassOverrides) != 2 {
		t.Fatalf("ClassOverrides count mismatch: %d", len(deserialized.ClassOverrides))
	}
	if deserialized.ClassOverrides["coe"]["POSTGRES_HOST"] != "chiwei-test-postgres" {
		t.Fatalf("coe POSTGRES_HOST mismatch: %q", deserialized.ClassOverrides["coe"]["POSTGRES_HOST"])
	}
	if deserialized.ClassOverrides["coe"]["POSTGRES_DB"] != "chiwei_test" {
		t.Fatalf("coe POSTGRES_DB mismatch: %q", deserialized.ClassOverrides["coe"]["POSTGRES_DB"])
	}
	if deserialized.ClassOverrides["private"]["POSTGRES_HOST"] != "private-postgres" {
		t.Fatalf("private POSTGRES_HOST mismatch: %q", deserialized.ClassOverrides["private"]["POSTGRES_HOST"])
	}

	// Verify RequiredKeys
	if len(deserialized.RequiredKeys) != 2 {
		t.Fatalf("RequiredKeys count mismatch: %d", len(deserialized.RequiredKeys))
	}
	if len(deserialized.RequiredKeys["coe"]) != 2 {
		t.Fatalf("coe RequiredKeys length mismatch: %d", len(deserialized.RequiredKeys["coe"]))
	}
	if len(deserialized.RequiredKeys["private"]) != 1 {
		t.Fatalf("private RequiredKeys length mismatch: %d", len(deserialized.RequiredKeys["private"]))
	}

	// Verify other fields survived
	if deserialized.Keys["POSTGRES_HOST"] != "prod-postgres" {
		t.Fatalf("Keys not preserved: %+v", deserialized.Keys)
	}
	if deserialized.LaneOverrides["dev"]["POSTGRES_PORT"] != "5433" {
		t.Fatalf("LaneOverrides not preserved: %+v", deserialized.LaneOverrides)
	}
}

// TestEmptyClassOverridesAndRequiredKeys tests with empty/nil values.
func TestEmptyClassOverridesAndRequiredKeys(t *testing.T) {
	bundle := &domain.ConfigBundle{
		Name: "simple-bundle",
		Keys: map[string]string{"KEY": "value"},
		// ClassOverrides and RequiredKeys intentionally empty
		CreatedAt: time.Now(),
		UpdatedAt: time.Now(),
	}

	model, err := bundleToModel(bundle)
	if err != nil {
		t.Fatalf("bundleToModel failed: %v", err)
	}

	// Empty maps should serialize to "{}" or ""
	deserialized, err := modelToBundle(model)
	if err != nil {
		t.Fatalf("modelToBundle failed: %v", err)
	}

	// Should have empty maps, not nil
	if deserialized.ClassOverrides == nil {
		t.Fatal("ClassOverrides should be empty map, not nil")
	}
	if deserialized.RequiredKeys == nil {
		t.Fatal("RequiredKeys should be empty map, not nil")
	}
	if len(deserialized.ClassOverrides) != 0 {
		t.Fatalf("ClassOverrides should be empty, got %d items", len(deserialized.ClassOverrides))
	}
	if len(deserialized.RequiredKeys) != 0 {
		t.Fatalf("RequiredKeys should be empty, got %d items", len(deserialized.RequiredKeys))
	}
}
