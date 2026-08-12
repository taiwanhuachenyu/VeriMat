import hashlib
import json

import pytest

from src.service.api import AuthRegistry
from src.service.tls import (
    ServerTLSConfig, TLSConfigurationError, build_server_tls_context, host_is_loopback,
)


def test_auth_registry_supports_exact_certificate_digest_and_rejects_ambiguity():
    certificate = b"DER certificate fixture"
    digest = hashlib.sha256(certificate).hexdigest()
    registry = AuthRegistry.from_json(json.dumps([{
        "client_cert_sha256": digest, "principal_id": "principal",
        "tenant_id": "tenant", "roles": ["read"],
    }]))
    assert registry.has_certificate_entries
    assert registry.authenticate_client_certificate(certificate).tenant_id == "tenant"
    assert registry.authenticate_client_certificate(b"other") is None
    with pytest.raises(ValueError, match="exactly one"):
        AuthRegistry.from_json(json.dumps([{
            "client_cert_sha256": digest, "token_sha256": digest,
            "principal_id": "principal", "tenant_id": "tenant", "roles": ["read"],
        }]))
    with pytest.raises(ValueError, match="unknown fields"):
        AuthRegistry.from_json(json.dumps([{
            "client_cert_sha256": digest, "principal_id": "principal",
            "tenant_id": "tenant", "roles": ["read"], "subject": "untrusted mapping",
        }]))


def test_tls_configuration_rejects_missing_ca_unsafe_key_and_symlinks(tmp_path):
    certificate = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    certificate.write_text("invalid but present")
    key.write_text("private")
    key.chmod(0o600)
    with pytest.raises(TLSConfigurationError, match="client CA"):
        build_server_tls_context(ServerTLSConfig(
            certificate=certificate, private_key=key,
            require_client_certificate=True,
        ))
    key.chmod(0o644)
    with pytest.raises(TLSConfigurationError, match="group/world"):
        build_server_tls_context(ServerTLSConfig(
            certificate=certificate, private_key=key,
        ))
    key.chmod(0o600)
    link = tmp_path / "linked.key"
    link.symlink_to(key)
    with pytest.raises(TLSConfigurationError, match="non-symlink"):
        build_server_tls_context(ServerTLSConfig(
            certificate=certificate, private_key=link,
        ))


def test_loopback_classification_is_narrow():
    assert all(host_is_loopback(value) for value in ("127.0.0.1", "::1", "localhost"))
    assert not host_is_loopback("0.0.0.0")
    assert not host_is_loopback("192.168.1.5")
