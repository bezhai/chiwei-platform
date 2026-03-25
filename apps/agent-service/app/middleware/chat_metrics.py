"""Prometheus metrics for chat pipeline stages."""

from prometheus_client import Counter, Histogram

# Buckets tuned for chat pipeline: fast path (100ms) to slow LLM (120s)
PIPELINE_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60, 120)

CHAT_PIPELINE_DURATION = Histogram(
    "chat_pipeline_duration_seconds",
    "Duration of each chat pipeline stage",
    ["stage"],
    buckets=PIPELINE_BUCKETS,
)

CHAT_FIRST_TOKEN = Histogram(
    "chat_first_token_seconds",
    "Time to first token from agent stream",
    buckets=(0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30),
)

CHAT_TOKENS = Counter(
    "chat_tokens_total",
    "Token count by type",
    ["type"],
)

CHAT_QUEUE_WAIT = Histogram(
    "chat_queue_wait_seconds",
    "Time spent waiting in MQ queue (chat_request)",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
