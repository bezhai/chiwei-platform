package middleware

import "net/http"

// Chain composes middlewares left-to-right: Chain(a, b, c)(h) == a(b(c(h)))
func Chain(middlewares ...func(http.Handler) http.Handler) func(http.Handler) http.Handler {
	return func(final http.Handler) http.Handler {
		for i := len(middlewares) - 1; i >= 0; i-- {
			final = middlewares[i](final)
		}
		return final
	}
}
