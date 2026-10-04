package main

import (
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/config"
)

// 构建执行器要拿到 GitPoller 已经在用的那个 GITHUB_TOKEN，以及决定 Secret 名字的泳道和前缀；
// 漏接任何一个，构建就静默退回匿名克隆。
func TestKanikoBuildConfig_PassesGitAuth(t *testing.T) {
	cfg := &config.Config{
		KanikoNamespace:           "paas-builds",
		GitHubToken:               "ghp_wiring_test",
		Lane:                      "ppe-auth",
		KanikoGitAuthSecretPrefix: "kaniko-git-auth",
	}

	got := kanikoBuildConfig(cfg)
	if got.GitToken != cfg.GitHubToken {
		t.Errorf("GitToken 没有接上 GITHUB_TOKEN")
	}
	if got.Lane != "ppe-auth" {
		t.Errorf("Lane = %q, want ppe-auth", got.Lane)
	}
	if got.GitAuthSecretPrefix != "kaniko-git-auth" {
		t.Errorf("GitAuthSecretPrefix = %q, want kaniko-git-auth", got.GitAuthSecretPrefix)
	}
	if got.Namespace != "paas-builds" {
		t.Errorf("Namespace = %q, want paas-builds", got.Namespace)
	}
}
