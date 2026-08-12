"""Fail-closed TLS context construction for the control plane."""
from __future__ import annotations

import os
import ssl
from dataclasses import dataclass
from pathlib import Path


class TLSConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class ServerTLSConfig:
    certificate: Path
    private_key: Path
    client_ca: Path | None = None
    require_client_certificate: bool = False


def _regular_file(label: str, value: str | Path) -> Path:
    path = Path(value)
    if path.is_symlink() or not path.is_file():
        raise TLSConfigurationError(f"{label} must be a regular non-symlink file")
    return path


def build_server_tls_context(config: ServerTLSConfig) -> ssl.SSLContext:
    certificate = _regular_file("TLS certificate", config.certificate)
    private_key = _regular_file("TLS private key", config.private_key)
    if private_key.stat().st_mode & 0o077:
        raise TLSConfigurationError("TLS private key must not be group/world accessible")
    client_ca = (
        _regular_file("client CA bundle", config.client_ca)
        if config.client_ca is not None else None
    )
    if config.require_client_certificate and client_ca is None:
        raise TLSConfigurationError("required client certificates need a client CA bundle")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.options |= ssl.OP_NO_COMPRESSION
    try:
        context.load_cert_chain(certfile=str(certificate), keyfile=str(private_key))
        if client_ca is not None:
            context.load_verify_locations(cafile=str(client_ca))
    except (OSError, ssl.SSLError) as exc:
        raise TLSConfigurationError(f"TLS credential loading failed: {exc}") from exc
    context.verify_mode = (
        ssl.CERT_REQUIRED if config.require_client_certificate
        else ssl.CERT_OPTIONAL if client_ca is not None else ssl.CERT_NONE
    )
    context.set_alpn_protocols(["http/1.1"])
    return context


def host_is_loopback(host: str) -> bool:
    return host.strip().lower() in {"127.0.0.1", "::1", "localhost"}
