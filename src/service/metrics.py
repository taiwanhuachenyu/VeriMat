"""Low-cardinality, secret-free Prometheus exposition for the control plane."""
from __future__ import annotations

import threading
from collections import Counter
from typing import Any

LATENCY_BUCKETS_SECONDS = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
)
ROUTE_LABELS = {
    "healthz", "readyz", "metrics", "jobs_collection", "job_item",
    "job_cancel", "not_found",
}
METHOD_LABELS = {"GET", "POST"}


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class ControlPlaneMetrics:
    """In-process request telemetry with a fixed label vocabulary.

    Job and usage gauges are read from SQLite at scrape time. No job ID, tenant, task, principal,
    provider route, prompt, or error message enters the exposition.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests: Counter[tuple[str, str, int]] = Counter()
        self._latency_buckets: Counter[tuple[str, str, float]] = Counter()
        self._latency_count: Counter[tuple[str, str]] = Counter()
        self._latency_sum: Counter[tuple[str, str]] = Counter()
        self._trace_write_failures = 0

    def record(
        self, *, method: str, route: str, status: int, duration_seconds: float,
    ) -> None:
        if method not in METHOD_LABELS or route not in ROUTE_LABELS:
            raise ValueError("metrics labels must come from the fixed vocabulary")
        if status < 100 or status > 599 or duration_seconds < 0:
            raise ValueError("metrics status or duration is invalid")
        with self._lock:
            self._requests[(method, route, status)] += 1
            self._latency_count[(method, route)] += 1
            self._latency_sum[(method, route)] += duration_seconds
            for boundary in LATENCY_BUCKETS_SECONDS:
                if duration_seconds <= boundary:
                    self._latency_buckets[(method, route, boundary)] += 1

    def record_trace_write_failure(self) -> None:
        with self._lock:
            self._trace_write_failures += 1

    def render(self, job_snapshot: dict[str, Any]) -> str:
        with self._lock:
            requests = dict(self._requests)
            buckets = dict(self._latency_buckets)
            counts = dict(self._latency_count)
            sums = dict(self._latency_sum)
            trace_write_failures = self._trace_write_failures
        lines = [
            "# HELP goai_control_http_requests_total Completed control-plane HTTP requests.",
            "# TYPE goai_control_http_requests_total counter",
        ]
        for (method, route, status), value in sorted(requests.items()):
            lines.append(
                'goai_control_http_requests_total{method="%s",route="%s",status="%d"} %d'
                % (_label(method), _label(route), status, value)
            )
        lines.extend([
            "# HELP goai_control_http_request_duration_seconds Control-plane request latency.",
            "# TYPE goai_control_http_request_duration_seconds histogram",
        ])
        for method, route in sorted(counts):
            for boundary in LATENCY_BUCKETS_SECONDS:
                value = buckets.get((method, route, boundary), 0)
                lines.append(
                    'goai_control_http_request_duration_seconds_bucket{method="%s",route="%s",le="%s"} %d'
                    % (_label(method), _label(route), boundary, value)
                )
            count = counts[(method, route)]
            lines.append(
                'goai_control_http_request_duration_seconds_bucket{method="%s",route="%s",le="+Inf"} %d'
                % (_label(method), _label(route), count)
            )
            lines.append(
                'goai_control_http_request_duration_seconds_count{method="%s",route="%s"} %d'
                % (_label(method), _label(route), count)
            )
            lines.append(
                'goai_control_http_request_duration_seconds_sum{method="%s",route="%s"} %.9f'
                % (_label(method), _label(route), sums[(method, route)])
            )

        lines.extend([
            "# HELP goai_control_jobs Current jobs by lifecycle status.",
            "# TYPE goai_control_jobs gauge",
        ])
        for status, value in sorted(job_snapshot["jobs_by_status"].items()):
            lines.append(f'goai_control_jobs{{status="{_label(status)}"}} {value}')
        lines.extend([
            "# HELP goai_control_jobs_by_stage Current jobs by execution stage.",
            "# TYPE goai_control_jobs_by_stage gauge",
        ])
        for stage, value in sorted(job_snapshot["jobs_by_stage"].items()):
            lines.append(f'goai_control_jobs_by_stage{{stage="{_label(stage)}"}} {value}')
        scalar_help = {
            "jobs_total": "Total current job rows.",
            "active_leases": "Current unexpired running leases.",
            "expired_leases": "Current expired running leases eligible for recovery.",
            "oldest_runnable_age_seconds": "Age of the oldest runnable job.",
            "used_calls": "Calls charged to current jobs.",
            "used_tokens": "Tokens charged to current jobs.",
            "used_cost_microunits": "Cost charged to current jobs in microunits.",
            "checkpoint_records": "Current durable checkpoint rows.",
            "usage_records": "Current usage-ledger rows.",
        }
        for name, description in scalar_help.items():
            metric = f"goai_control_{name}"
            lines.extend([
                f"# HELP {metric} {description}", f"# TYPE {metric} gauge",
                f"{metric} {job_snapshot[name]}",
            ])
        lines.extend([
            "# HELP goai_control_trace_write_failures_total Structured trace write failures.",
            "# TYPE goai_control_trace_write_failures_total counter",
            f"goai_control_trace_write_failures_total {trace_write_failures}",
        ])
        return "\n".join(lines) + "\n"
