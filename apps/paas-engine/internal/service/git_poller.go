package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

// GitPoller 轮询 GitHub API 检测新 commit，自动触发 CI pipeline。
type GitPoller struct {
	ciConfigRepo port.CIConfigRepository
	pipelineSvc  *PipelineService
	httpClient   *http.Client
	owner        string
	repo         string
	token        string
	interval     time.Duration
}

// NewGitPoller 创建 GitPoller。ownerRepo 格式如 "bezhai/chiwei-platform.git"。
func NewGitPoller(
	ciConfigRepo port.CIConfigRepository,
	pipelineSvc *PipelineService,
	ownerRepo string,
	token string,
	interval time.Duration,
	proxyURL string,
) *GitPoller {
	// 解析 owner/repo（去掉 .git 后缀）
	ownerRepo = strings.TrimSuffix(ownerRepo, ".git")
	parts := strings.SplitN(ownerRepo, "/", 2)
	if len(parts) != 2 {
		slog.Error("invalid owner/repo format", "ownerRepo", ownerRepo)
		return nil
	}

	transport := http.DefaultTransport.(*http.Transport).Clone()
	if proxyURL != "" {
		if u, err := url.Parse(proxyURL); err == nil {
			transport.Proxy = http.ProxyURL(u)
		}
	}

	return &GitPoller{
		ciConfigRepo: ciConfigRepo,
		pipelineSvc:  pipelineSvc,
		httpClient:   &http.Client{Transport: transport, Timeout: 15 * time.Second},
		owner:        parts[0],
		repo:         parts[1],
		token:        token,
		interval:     interval,
	}
}

// Start 启动轮询循环，ctx 取消时退出。
func (p *GitPoller) Start(ctx context.Context) {
	slog.Info("git poller started", "owner", p.owner, "repo", p.repo, "interval", p.interval)

	// 启动时立即 poll 一次
	p.pollAll(ctx)

	ticker := time.NewTicker(p.interval)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			slog.Info("git poller stopped")
			return
		case <-ticker.C:
			p.pollAll(ctx)
		}
	}
}

// pollAll 遍历所有活跃 CIConfig，轮询各分支的最新 commit。
func (p *GitPoller) pollAll(ctx context.Context) {
	configs, err := p.ciConfigRepo.FindActive(ctx)
	if err != nil {
		slog.Error("git poller: failed to list active ci configs", "error", err)
		return
	}

	for _, cfg := range configs {
		// 跳过 main/master 分支
		if cfg.Branch == "main" || cfg.Branch == "master" {
			continue
		}
		p.pollBranch(ctx, cfg.Lane, cfg.Branch)
	}
}

// pollBranch 检查单个分支的最新 commit，若有新 commit 则触发 pipeline。
func (p *GitPoller) pollBranch(ctx context.Context, lane, branch string) {
	sha, err := p.fetchBranchHead(ctx, branch)
	if err != nil {
		slog.Warn("git poller: failed to fetch branch head", "branch", branch, "error", err)
		return
	}

	_, err = p.pipelineSvc.TriggerPipeline(ctx, lane, TriggerPipelineRequest{
		CommitSHA: sha,
	})
	if err != nil {
		// 幂等：同一 SHA 已触发过，静默跳过
		if errors.Is(err, domain.ErrAlreadyExists) {
			return
		}
		slog.Warn("git poller: failed to trigger pipeline", "lane", lane, "branch", branch, "sha", sha, "error", err)
		return
	}

	slog.Info("git poller: triggered pipeline", "lane", lane, "branch", branch, "sha", sha)
}

// branchResponse 是 GitHub API 返回的分支信息（仅取所需字段）。
type branchResponse struct {
	Commit struct {
		SHA string `json:"sha"`
	} `json:"commit"`
}

// fetchBranchHead 调用 GitHub API 获取分支最新 commit SHA。
func (p *GitPoller) fetchBranchHead(ctx context.Context, branch string) (string, error) {
	apiURL := fmt.Sprintf("https://api.github.com/repos/%s/%s/branches/%s", p.owner, p.repo, branch)

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, apiURL, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("Authorization", "Bearer "+p.token)
	req.Header.Set("Accept", "application/vnd.github.v3+json")

	resp, err := p.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("github api request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("github api returned %d for branch %s", resp.StatusCode, branch)
	}

	var result branchResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("failed to decode github response: %w", err)
	}

	if result.Commit.SHA == "" {
		return "", fmt.Errorf("empty commit sha for branch %s", branch)
	}

	return result.Commit.SHA, nil
}
