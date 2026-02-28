package route

// FallbackRoutes returns hardcoded routes used when YAML config is unavailable.
func FallbackRoutes() []Route {
	routes := []Route{
		{Prefix: "/api/paas/", Service: "paas-engine", Port: 8080, StripPrefix: "/api/paas", RewritePrefix: "/api/v1"},
		{Prefix: "/webhook/", Service: "lark-proxy", Port: 3003},
		{Prefix: "/dashboard/api/", Service: "monitor-dashboard", Port: 3002},
		{Prefix: "/dashboard/", Service: "monitor-dashboard-web", Port: 80},
	}
	sortRoutes(routes)
	return routes
}
