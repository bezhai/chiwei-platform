package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"strings"
	"time"
)

func main() {
	port := envOr("HTTP_PORT", "8080")
	feishuURL := os.Getenv("FEISHU_WEBHOOK_URL")
	if feishuURL == "" {
		slog.Error("FEISHU_WEBHOOK_URL is required")
		os.Exit(1)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("ok"))
	})
	mux.HandleFunc("/webhook", webhookHandler(feishuURL))

	slog.Info("alert-webhook listening", "port", port)
	if err := http.ListenAndServe(":"+port, mux); err != nil {
		slog.Error("server error", "error", err)
		os.Exit(1)
	}
}

// AlertManager webhook payload
type AlertManagerPayload struct {
	Status string  `json:"status"`
	Alerts []Alert `json:"alerts"`
}

type Alert struct {
	Status      string            `json:"status"`
	Labels      map[string]string `json:"labels"`
	Annotations map[string]string `json:"annotations"`
	StartsAt    time.Time         `json:"startsAt"`
	EndsAt      time.Time         `json:"endsAt"`
}

func webhookHandler(feishuURL string) http.HandlerFunc {
	client := &http.Client{Timeout: 10 * time.Second}

	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}

		var payload AlertManagerPayload
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			slog.Error("failed to decode payload", "error", err)
			http.Error(w, "bad request", http.StatusBadRequest)
			return
		}

		for _, alert := range payload.Alerts {
			card := buildFeishuCard(alert)
			body, _ := json.Marshal(card)

			resp, err := client.Post(feishuURL, "application/json", bytes.NewReader(body))
			if err != nil {
				slog.Error("failed to send feishu message", "alert", alert.Labels["alertname"], "error", err)
				continue
			}
			resp.Body.Close()

			if resp.StatusCode != http.StatusOK {
				slog.Warn("feishu returned non-200", "status", resp.StatusCode, "alert", alert.Labels["alertname"])
			} else {
				slog.Info("alert sent", "alert", alert.Labels["alertname"], "status", alert.Status)
			}
		}

		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	}
}

func buildFeishuCard(alert Alert) map[string]interface{} {
	firing := alert.Status == "firing"

	// Color: red for firing, green for resolved
	color := "green"
	statusText := "已恢复 ✅"
	if firing {
		color = "red"
		statusText = "告警触发 🔥"
		severity := alert.Labels["severity"]
		if severity == "critical" {
			statusText = "严重告警 🚨"
		}
	}

	alertName := alert.Labels["alertname"]
	namespace := alert.Labels["namespace"]
	summary := alert.Annotations["summary"]
	description := alert.Annotations["description"]

	// Build content fields
	var fields []map[string]interface{}

	fields = append(fields, map[string]interface{}{
		"is_short": true,
		"text": map[string]interface{}{
			"tag":     "lark_md",
			"content": fmt.Sprintf("**状态:** %s", statusText),
		},
	})

	if namespace != "" {
		fields = append(fields, map[string]interface{}{
			"is_short": true,
			"text": map[string]interface{}{
				"tag":     "lark_md",
				"content": fmt.Sprintf("**Namespace:** %s", namespace),
			},
		})
	}

	if severity := alert.Labels["severity"]; severity != "" {
		fields = append(fields, map[string]interface{}{
			"is_short": true,
			"text": map[string]interface{}{
				"tag":     "lark_md",
				"content": fmt.Sprintf("**级别:** %s", severity),
			},
		})
	}

	fields = append(fields, map[string]interface{}{
		"is_short": false,
		"text": map[string]interface{}{
			"tag":     "lark_md",
			"content": fmt.Sprintf("**详情:** %s", description),
		},
	})

	if firing {
		fields = append(fields, map[string]interface{}{
			"is_short": true,
			"text": map[string]interface{}{
				"tag":     "lark_md",
				"content": fmt.Sprintf("**触发时间:** %s", alert.StartsAt.In(time.FixedZone("CST", 8*3600)).Format("01-02 15:04:05")),
			},
		})
	} else {
		fields = append(fields, map[string]interface{}{
			"is_short": true,
			"text": map[string]interface{}{
				"tag":     "lark_md",
				"content": fmt.Sprintf("**恢复时间:** %s", alert.EndsAt.In(time.FixedZone("CST", 8*3600)).Format("01-02 15:04:05")),
			},
		})
	}

	// Build labels summary (exclude common ones)
	var labelParts []string
	for k, v := range alert.Labels {
		if k == "alertname" || k == "severity" || k == "namespace" {
			continue
		}
		labelParts = append(labelParts, fmt.Sprintf("%s=%s", k, v))
	}
	if len(labelParts) > 0 {
		fields = append(fields, map[string]interface{}{
			"is_short": false,
			"text": map[string]interface{}{
				"tag":     "lark_md",
				"content": fmt.Sprintf("**标签:** %s", strings.Join(labelParts, ", ")),
			},
		})
	}

	title := fmt.Sprintf("[%s] %s", alertName, summary)
	if len(title) > 80 {
		title = title[:80]
	}

	return map[string]interface{}{
		"msg_type": "interactive",
		"card": map[string]interface{}{
			"header": map[string]interface{}{
				"title": map[string]interface{}{
					"tag":     "plain_text",
					"content": title,
				},
				"template": color,
			},
			"elements": []interface{}{
				map[string]interface{}{
					"tag":    "div",
					"fields": fields,
				},
			},
		},
	}
}

func envOr(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
