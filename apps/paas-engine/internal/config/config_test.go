package config

import "testing"

// 构建 Job 的 git 凭据 Secret 按实例所在泳道命名：LANE 由 deployer 注入每个 Release，
// 前缀可配，不配时用默认值。
func TestLoad_GitAuthSecretForBuilds(t *testing.T) {
	t.Setenv("LANE", "ppe-auth")
	t.Setenv("KANIKO_GIT_AUTH_SECRET_PREFIX", "")

	cfg := Load()
	if cfg.Lane != "ppe-auth" {
		t.Errorf("Lane = %q, want ppe-auth", cfg.Lane)
	}
	if cfg.KanikoGitAuthSecretPrefix != "kaniko-git-auth" {
		t.Errorf("KanikoGitAuthSecretPrefix = %q, want default kaniko-git-auth", cfg.KanikoGitAuthSecretPrefix)
	}

	t.Setenv("KANIKO_GIT_AUTH_SECRET_PREFIX", "custom-git-auth")
	if got := Load().KanikoGitAuthSecretPrefix; got != "custom-git-auth" {
		t.Errorf("KanikoGitAuthSecretPrefix = %q, want custom-git-auth", got)
	}
}
