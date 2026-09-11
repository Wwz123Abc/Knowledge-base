from prometheus_client import Counter, Histogram

HTTP_REQUESTS = Counter(
    "rag_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
HTTP_REQUEST_DURATION = Histogram(
    "rag_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "path"],
)
RETRIEVAL_DURATION = Histogram(
    "rag_retrieval_duration_seconds",
    "Hybrid retrieval latency",
)
ANSWER_DURATION = Histogram(
    "rag_answer_duration_seconds",
    "End-to-end answer latency",
)
INGESTION_JOBS = Counter(
    "rag_ingestion_jobs_total",
    "Ingestion job results",
    ["status"],
)
