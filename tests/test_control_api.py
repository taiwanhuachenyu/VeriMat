import hashlib
import http.client
import json
import threading

import pytest

from src.service.api import AuthRegistry, ControlPlaneApp
from src.service.rate_limit import PrincipalRateLimiter
from src.service.tracing import StructuredTraceLog


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _auth():
    return AuthRegistry.from_json(json.dumps([
        {
            "token_sha256": _digest("token-a"), "principal_id": "a-admin",
            "tenant_id": "tenant-a", "roles": ["admin"],
        },
        {
            "token_sha256": _digest("token-b"), "principal_id": "b-reader",
            "tenant_id": "tenant-b", "roles": ["read"],
        },
    ]))


class RunningServer:
    def __init__(self, tmp_path, *, rate_limiter=None, trace_recorder=None):
        app = ControlPlaneApp(
            store_path=tmp_path / "jobs.db", auth=_auth(), rate_limiter=rate_limiter,
            trace_recorder=trace_recorder,
        )
        self.server = app.make_server("127.0.0.1", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method, path, *, token=None, body=None, headers=None):
        connection = http.client.HTTPConnection(self.host, self.port, timeout=3)
        request_headers = dict(headers or {})
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=request_headers)
        response = connection.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type", "")
        value = json.loads(raw) if content_type.startswith("application/json") else raw.decode()
        connection.close()
        return response.status, value, dict(response.getheaders())


def _submission(**overrides):
    value = {
        "idempotency_key": "request-1", "task": "Assess a materials claim",
        "max_calls": 10, "max_tokens": 1000, "max_cost_microunits": 100,
    }
    value.update(overrides)
    return value


def test_library_api_also_refuses_nonloopback_plaintext_listener(tmp_path):
    app = ControlPlaneApp(store_path=tmp_path / "jobs.db", auth=_auth())
    with pytest.raises(ValueError, match="require TLS"):
        app.make_server("0.0.0.0", 0)


def test_health_and_authentication_are_fail_closed(tmp_path):
    server = RunningServer(tmp_path)
    try:
        status, body, headers = server.request("GET", "/healthz")
        assert status == 200 and body["status"] == "ok"
        assert headers["Cache-Control"] == "no-store"
        status, body, _ = server.request("GET", "/readyz")
        assert status == 200 and body["status"] == "ready"
        status, body, _ = server.request("POST", "/v1/jobs", body=_submission())
        assert status == 401
        assert body["error"]["code"] == "UNAUTHENTICATED"
    finally:
        server.close()


def test_submit_is_idempotent_and_cancel_is_idempotent(tmp_path):
    server = RunningServer(tmp_path)
    try:
        first = server.request("POST", "/v1/jobs", token="token-a", body=_submission())
        second = server.request("POST", "/v1/jobs", token="token-a", body=_submission())
        assert first[0] == second[0] == 200
        assert first[1]["job"]["job_id"] == second[1]["job"]["job_id"]
        job_id = first[1]["job"]["job_id"]
        cancelled = server.request("POST", f"/v1/jobs/{job_id}/cancel", token="token-a")
        repeated = server.request("POST", f"/v1/jobs/{job_id}/cancel", token="token-a")
        assert cancelled[1]["job"]["status"] == "CANCELLED"
        assert repeated[1]["job"]["status"] == "CANCELLED"
    finally:
        server.close()


def test_cross_tenant_read_is_indistinguishable_from_missing(tmp_path):
    server = RunningServer(tmp_path)
    try:
        created = server.request("POST", "/v1/jobs", token="token-a", body=_submission())
        job_id = created[1]["job"]["job_id"]
        cross = server.request("GET", f"/v1/jobs/{job_id}", token="token-b")
        missing = server.request(
            "GET", "/v1/jobs/00000000-0000-0000-0000-000000000000", token="token-b"
        )
        assert cross[0] == missing[0] == 404
        assert cross[1]["error"] == missing[1]["error"]
    finally:
        server.close()


def test_roles_and_input_limits_are_enforced(tmp_path):
    server = RunningServer(tmp_path)
    try:
        status, body, _ = server.request(
            "POST", "/v1/jobs", token="token-b", body=_submission()
        )
        assert status == 403 and body["error"]["code"] == "FORBIDDEN"
        status, body, _ = server.request(
            "POST", "/v1/jobs", token="token-a", body=_submission(max_calls=10001)
        )
        assert status == 400 and body["error"]["code"] == "INVALID_REQUEST"
    finally:
        server.close()


def test_metrics_are_admin_only_low_cardinality_and_secret_free(tmp_path):
    server = RunningServer(tmp_path)
    try:
        assert server.request("GET", "/metrics")[0] == 401
        assert server.request("GET", "/metrics", token="token-b")[0] == 403
        created = server.request(
            "POST", "/v1/jobs", token="token-a",
            body=_submission(task="private prompt material", idempotency_key="private-key"),
        )
        job_id = created[1]["job"]["job_id"]
        server.request("GET", f"/v1/jobs/{job_id}", token="token-a")
        status, metrics, headers = server.request("GET", "/metrics", token="token-a")
        assert status == 200
        assert headers["Content-Type"].startswith("text/plain")
        assert 'route="jobs_collection"' in metrics
        assert 'status="QUEUED"} 1' in metrics
        assert "private prompt material" not in metrics
        assert "private-key" not in metrics
        assert "tenant-a" not in metrics and job_id not in metrics
        assert "token-a" not in metrics
    finally:
        server.close()


def test_principal_rate_limit_returns_retry_after_but_health_remains_available(tmp_path):
    limiter = PrincipalRateLimiter(requests_per_minute=1, burst=2)
    server = RunningServer(tmp_path, rate_limiter=limiter)
    try:
        assert server.request("POST", "/v1/jobs", token="token-a", body=_submission())[0] == 200
        assert server.request("GET", "/metrics", token="token-a")[0] == 200
        status, body, headers = server.request("GET", "/metrics", token="token-a")
        assert status == 429 and body["error"]["code"] == "RATE_LIMITED"
        assert int(headers["Retry-After"]) >= 1
        assert server.request("GET", "/healthz")[0] == 200
        assert limiter.configured_bucket_count() == 1
    finally:
        server.close()


def test_traceparent_is_propagated_and_persisted_trace_excludes_sensitive_values(tmp_path):
    trace_path = tmp_path / "control-traces.jsonl"
    server = RunningServer(tmp_path, trace_recorder=StructuredTraceLog(trace_path))
    upstream_trace = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    try:
        status, _, headers = server.request(
            "POST", "/v1/jobs", token="token-a",
            body=_submission(task="private trace task", idempotency_key="private-trace-key"),
            headers={"traceparent": upstream_trace, "X-Request-ID": "private-request-id"},
        )
        assert status == 200
        assert headers["traceparent"].startswith(
            "00-0123456789abcdef0123456789abcdef-"
        )
    finally:
        server.close()
    content = trace_path.read_text(encoding="utf-8")
    row = json.loads(content)
    assert row["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert row["parent_span_id"] == "0123456789abcdef"
    assert row["route"] == "jobs_collection" and row["status"] == 200
    for forbidden in (
        "private trace task", "private-trace-key", "private-request-id",
        "token-a", "tenant-a", "a-admin",
    ):
        assert forbidden not in content
