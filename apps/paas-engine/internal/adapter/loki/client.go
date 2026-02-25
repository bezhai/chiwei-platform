package loki

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

// Client 通过 Loki HTTP API 查询构建日志。
type Client struct {
	baseURL    string
	httpClient *http.Client
}

func NewClient(baseURL string) *Client {
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		httpClient: &http.Client{
			Timeout: 30 * time.Second,
		},
	}
}

// QueryBuildLogs 查询指定 build 的 kaniko Pod 日志。
// buildID 中的 "-" 会被去除以匹配 kaniko job 名称模式。
func (c *Client) QueryBuildLogs(ctx context.Context, namespace, buildID string, start, end time.Time) (string, error) {
	podPrefix := "kaniko-" + strings.ReplaceAll(buildID, "-", "")
	query := fmt.Sprintf(`{namespace=%q, pod=~%q}`, namespace, podPrefix+".*")

	params := url.Values{
		"query":     {query},
		"start":     {fmt.Sprintf("%d", start.UnixNano())},
		"end":       {fmt.Sprintf("%d", end.UnixNano())},
		"direction": {"forward"},
		"limit":     {"5000"},
	}

	reqURL := c.baseURL + "/loki/api/v1/query_range?" + params.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return "", fmt.Errorf("loki: build request: %w", err)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		return "", fmt.Errorf("loki: request failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("loki: unexpected status %d", resp.StatusCode)
	}

	var result queryRangeResponse
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return "", fmt.Errorf("loki: decode response: %w", err)
	}

	if result.Status != "success" {
		return "", fmt.Errorf("loki: query status %q", result.Status)
	}

	return extractLogs(result.Data), nil
}

// Loki query_range 响应结构（只建模需要的字段）。

type queryRangeResponse struct {
	Status string         `json:"status"`
	Data   queryRangeData `json:"data"`
}

type queryRangeData struct {
	ResultType string   `json:"resultType"`
	Result     []stream `json:"result"`
}

type stream struct {
	Values [][]string `json:"values"` // [[timestamp_ns, line], ...]
}

type logEntry struct {
	ts   string
	line string
}

// extractLogs 从所有 stream 中提取日志行，按时间戳排序后拼接。
func extractLogs(data queryRangeData) string {
	var entries []logEntry
	for _, s := range data.Result {
		for _, v := range s.Values {
			if len(v) >= 2 {
				entries = append(entries, logEntry{ts: v[0], line: v[1]})
			}
		}
	}

	sort.Slice(entries, func(i, j int) bool {
		return entries[i].ts < entries[j].ts
	})

	var b strings.Builder
	for _, e := range entries {
		b.WriteString(e.line)
		b.WriteByte('\n')
	}
	return b.String()
}
