package kubernetes

import (
	"context"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	batchv1 "k8s.io/api/batch/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func TestSubmit_ContextDirArgs(t *testing.T) {
	tests := []struct {
		name       string
		contextDir string
		wantArg    string // 期望包含的参数
		wantAbsent string // 期望不包含的参数前缀
	}{
		{
			name:       "子目录构建：使用 --context-sub-path",
			contextDir: "apps/lark-proxy",
			wantArg:    "--context-sub-path=apps/lark-proxy",
		},
		{
			name:       "空 context_dir：不追加子路径",
			contextDir: "",
			wantAbsent: "--context-sub-path=",
		},
		{
			name:       "根目录构建(.)：不追加子路径",
			contextDir: ".",
			wantAbsent: "--context-sub-path=",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			client := fake.NewSimpleClientset()
			executor := NewKanikoBuildExecutor(client, KanikoBuildConfig{
				Namespace:   "paas-builds",
				KanikoImage: "gcr.io/kaniko-project/executor:latest",
			})

			sub := &port.BuildSubmission{
				BuildID:    "test-build-id",
				GitRepo:    "https://github.com/example/repo",
				GitRef:     "main",
				ImageTag:   "registry.example.com/app:latest",
				ContextDir: tt.contextDir,
			}

			_, err := executor.Submit(context.Background(), sub)
			if err != nil {
				t.Fatalf("Submit() error = %v", err)
			}

			jobs, err := client.BatchV1().Jobs("paas-builds").List(context.Background(), metav1.ListOptions{})
			if err != nil {
				t.Fatalf("List jobs error = %v", err)
			}
			if len(jobs.Items) != 1 {
				t.Fatalf("expected 1 job, got %d", len(jobs.Items))
			}

			args := jobs.Items[0].Spec.Template.Spec.Containers[0].Args

			if tt.wantArg != "" {
				if !containsArg(args, tt.wantArg) {
					t.Errorf("expected args to contain %q, got %v", tt.wantArg, args)
				}
			}

			if tt.wantAbsent != "" {
				if containsArgPrefix(args, tt.wantAbsent) {
					t.Errorf("expected args NOT to contain prefix %q, got %v", tt.wantAbsent, args)
				}
			}
		})
	}
}

func TestJobToStatus(t *testing.T) {
	tests := []struct {
		name       string
		job        *batchv1.Job
		wantStatus domain.BuildStatus
	}{
		{
			name: "job succeeded",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Conditions: []batchv1.JobCondition{
						{Type: batchv1.JobComplete, Status: "True"},
					},
				},
			},
			wantStatus: domain.BuildStatusSucceeded,
		},
		{
			name: "job failed",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Conditions: []batchv1.JobCondition{
						{Type: batchv1.JobFailed, Status: "True"},
					},
				},
			},
			wantStatus: domain.BuildStatusFailed,
		},
		{
			name: "job running",
			job: &batchv1.Job{
				Status: batchv1.JobStatus{
					Active: 1,
				},
			},
			wantStatus: domain.BuildStatusRunning,
		},
		{
			name:       "job pending (no conditions, no active)",
			job:        &batchv1.Job{},
			wantStatus: "",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			status, _ := jobToStatus(tt.job)
			if status != tt.wantStatus {
				t.Errorf("jobToStatus() = %v, want %v", status, tt.wantStatus)
			}
		})
	}
}

func containsArg(args []string, target string) bool {
	for _, a := range args {
		if a == target {
			return true
		}
	}
	return false
}

func containsArgPrefix(args []string, prefix string) bool {
	for _, a := range args {
		if len(a) >= len(prefix) && a[:len(prefix)] == prefix {
			return true
		}
	}
	return false
}
