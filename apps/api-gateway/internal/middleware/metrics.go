package middleware

import (
	"net/http"
	"strconv"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	httpRequestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "http_requests_total",
		Help: "Total number of HTTP requests.",
	}, []string{"method", "status"})

	httpRequestDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "http_request_duration_seconds",
		Help:    "HTTP request duration in seconds.",
		Buckets: prometheus.DefBuckets,
	}, []string{"method"})

	httpRequestsInFlight = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "http_requests_in_flight",
		Help: "Number of HTTP requests currently being processed.",
	})

	ProxyRequestsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "gateway_proxy_requests_total",
		Help: "Total number of proxied requests by service and status.",
	}, []string{"service", "status"})

	ProxyDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "gateway_proxy_duration_seconds",
		Help:    "Proxy request duration in seconds.",
		Buckets: prometheus.DefBuckets,
	}, []string{"service"})
)

// Metrics is an HTTP middleware that records request metrics.
func Metrics(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		httpRequestsInFlight.Inc()
		defer httpRequestsInFlight.Dec()

		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sw, r)

		httpRequestsTotal.WithLabelValues(r.Method, strconv.Itoa(sw.status)).Inc()
		httpRequestDuration.WithLabelValues(r.Method).Observe(time.Since(start).Seconds())
	})
}
