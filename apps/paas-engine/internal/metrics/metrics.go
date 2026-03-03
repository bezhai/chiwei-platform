package metrics

import (
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
)

var (
	BuildsTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "paas_builds_total",
		Help: "Total number of builds by status.",
	}, []string{"status"})

	BuildsInProgress = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "paas_builds_in_progress",
		Help: "Number of builds currently in progress.",
	})

	ReleasesTotal = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "paas_releases_total",
		Help: "Total number of releases by lane.",
	}, []string{"lane"})
)
