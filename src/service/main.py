"""Command-line entrypoint for the tenant-scoped control API."""
from __future__ import annotations

import argparse
import os

from .api import AuthRegistry, ControlPlaneApp
from .rate_limit import PrincipalRateLimiter
from .tracing import StructuredTraceLog
from .tls import (
    ServerTLSConfig, TLSConfigurationError, build_server_tls_context, host_is_loopback,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--database", required=True)
    parser.add_argument("--rate-limit-per-minute", type=int, default=600)
    parser.add_argument("--rate-limit-burst", type=int, default=100)
    parser.add_argument("--trace-log")
    parser.add_argument("--tls-certificate")
    parser.add_argument("--tls-private-key")
    parser.add_argument("--tls-client-ca")
    parser.add_argument("--require-client-certificate", action="store_true")
    args = parser.parse_args(argv)
    raw_auth = os.environ.get("GOAI_AUTH_PRINCIPALS_JSON", "")
    if not raw_auth:
        parser.error("GOAI_AUTH_PRINCIPALS_JSON is required")
    try:
        auth = AuthRegistry.from_json(raw_auth)
    except ValueError as exc:
        parser.error(str(exc))
    try:
        limiter = PrincipalRateLimiter(
            requests_per_minute=args.rate_limit_per_minute,
            burst=args.rate_limit_burst,
        )
    except ValueError as exc:
        parser.error(str(exc))
    try:
        trace_recorder = StructuredTraceLog(args.trace_log) if args.trace_log else None
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    tls_values = (args.tls_certificate, args.tls_private_key)
    if any(tls_values) and not all(tls_values):
        parser.error("TLS certificate and private key must be configured together")
    if not all(tls_values) and not host_is_loopback(args.host):
        parser.error("non-loopback listeners require TLS")
    if args.tls_client_ca and not all(tls_values):
        parser.error("client CA configuration requires TLS")
    if args.require_client_certificate and not args.tls_client_ca:
        parser.error("required client certificates need --tls-client-ca")
    if auth.has_certificate_entries and not args.require_client_certificate:
        parser.error("client certificate principals require mandatory mTLS")
    try:
        tls_context = build_server_tls_context(ServerTLSConfig(
            certificate=args.tls_certificate,
            private_key=args.tls_private_key,
            client_ca=args.tls_client_ca,
            require_client_certificate=args.require_client_certificate,
        )) if all(tls_values) else None
    except TLSConfigurationError as exc:
        parser.error(str(exc))
    server = ControlPlaneApp(
        store_path=args.database, auth=auth, rate_limiter=limiter,
        trace_recorder=trace_recorder,
    ).make_server(args.host, args.port, tls_context=tls_context)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
