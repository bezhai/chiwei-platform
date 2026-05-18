package repository

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// TestAppToModel_AllowedLaneClasses tests serialization of AllowedLaneClasses.
func TestAppToModel_AllowedLaneClasses(t *testing.T) {
	app := &domain.App{
		Name:               "channel-proxy-test",
		ImageRepoName:      "channel-proxy",
		Port:               3003,
		AllowedLaneClasses: []string{"prod"},
		CreatedAt:          time.Now(),
		UpdatedAt:          time.Now(),
	}

	model, err := appToModel(app)
	if err != nil {
		t.Fatalf("appToModel failed: %v", err)
	}

	// Verify AllowedLaneClasses JSON is valid
	if model.AllowedLaneClasses == "" {
		t.Fatal("AllowedLaneClasses should not be empty")
	}
	var allowedLaneClasses []string
	if err := json.Unmarshal([]byte(model.AllowedLaneClasses), &allowedLaneClasses); err != nil {
		t.Fatalf("failed to unmarshal AllowedLaneClasses JSON: %v", err)
	}
	if len(allowedLaneClasses) != 1 || allowedLaneClasses[0] != "prod" {
		t.Fatalf("AllowedLaneClasses not preserved: %+v", allowedLaneClasses)
	}
}

// TestModelToApp_AllowedLaneClasses tests deserialization.
func TestModelToApp_AllowedLaneClasses(t *testing.T) {
	allowedLaneClassesJSON := `["prod"]`

	model := &AppModel{
		Name:                  "channel-proxy-test",
		ImageRepoName:         "channel-proxy",
		Port:                  3003,
		AllowedLaneClasses:    allowedLaneClassesJSON,
		CreatedAt:             time.Now(),
		UpdatedAt:             time.Now(),
	}

	app, err := modelToApp(model)
	if err != nil {
		t.Fatalf("modelToApp failed: %v", err)
	}

	if len(app.AllowedLaneClasses) != 1 || app.AllowedLaneClasses[0] != "prod" {
		t.Fatalf("AllowedLaneClasses not deserialized: %+v", app.AllowedLaneClasses)
	}
}

// TestAppRoundTrip_AllowedLaneClasses tests appToModel then modelToApp.
func TestAppRoundTrip_AllowedLaneClasses(t *testing.T) {
	original := &domain.App{
		Name:               "agent-service",
		ImageRepoName:      "agent-service",
		Port:               8000,
		AllowedLaneClasses: []string{"coe", "ppe", "prod"},
		CreatedAt:          time.Now(),
		UpdatedAt:          time.Now(),
	}

	// Serialize to model
	model, err := appToModel(original)
	if err != nil {
		t.Fatalf("appToModel failed: %v", err)
	}

	// Deserialize back to app
	deserialized, err := modelToApp(model)
	if err != nil {
		t.Fatalf("modelToApp failed: %v", err)
	}

	// Verify AllowedLaneClasses
	if len(deserialized.AllowedLaneClasses) != 3 {
		t.Fatalf("AllowedLaneClasses count mismatch: got %d, want 3", len(deserialized.AllowedLaneClasses))
	}
	expectedClasses := map[string]bool{"coe": true, "ppe": true, "prod": true}
	for _, cls := range deserialized.AllowedLaneClasses {
		if !expectedClasses[cls] {
			t.Fatalf("unexpected AllowedLaneClasses value: %q", cls)
		}
	}

	// Verify other fields survived
	if deserialized.Name != original.Name {
		t.Fatalf("Name not preserved: %q", deserialized.Name)
	}
	if deserialized.ImageRepoName != original.ImageRepoName {
		t.Fatalf("ImageRepoName not preserved: %q", deserialized.ImageRepoName)
	}
	if deserialized.Port != original.Port {
		t.Fatalf("Port not preserved: %d", deserialized.Port)
	}
}

// TestAppRoundTrip_EmptyAllowedLaneClasses tests that nil AllowedLaneClasses
// remains nil after a full round-trip serialization.
func TestAppRoundTrip_EmptyAllowedLaneClasses(t *testing.T) {
	app := &domain.App{
		Name:           "simple-app",
		ImageRepoName:  "simple-image",
		Port:           8080,
		// AllowedLaneClasses intentionally nil
		CreatedAt: time.Now(),
		UpdatedAt: time.Now(),
	}

	// Serialize to model
	model, err := appToModel(app)
	if err != nil {
		t.Fatalf("appToModel failed: %v", err)
	}

	// Deserialize back to app
	deserialized, err := modelToApp(model)
	if err != nil {
		t.Fatalf("modelToApp failed: %v", err)
	}

	// Round-trip should preserve nil AllowedLaneClasses as nil
	if deserialized.AllowedLaneClasses != nil {
		t.Fatalf("AllowedLaneClasses should be nil, got %+v", deserialized.AllowedLaneClasses)
	}
}
