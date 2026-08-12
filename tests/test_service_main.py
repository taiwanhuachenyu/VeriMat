import hashlib
import json

import pytest

from src.service.main import main


def _registry(field="token_sha256"):
    return json.dumps([{
        field: hashlib.sha256(b"credential").hexdigest(),
        "principal_id": "principal", "tenant_id": "tenant", "roles": ["admin"],
    }])


def test_nonloopback_plaintext_listener_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("GOAI_AUTH_PRINCIPALS_JSON", _registry())
    with pytest.raises(SystemExit) as raised:
        main(["--host", "0.0.0.0", "--database", str(tmp_path / "jobs.db")])
    assert raised.value.code == 2


def test_partial_tls_configuration_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("GOAI_AUTH_PRINCIPALS_JSON", _registry())
    with pytest.raises(SystemExit) as raised:
        main([
            "--database", str(tmp_path / "jobs.db"),
            "--tls-certificate", str(tmp_path / "server.crt"),
        ])
    assert raised.value.code == 2


def test_certificate_principal_cannot_start_without_mandatory_mtls(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "GOAI_AUTH_PRINCIPALS_JSON", _registry("client_cert_sha256"),
    )
    with pytest.raises(SystemExit) as raised:
        main(["--database", str(tmp_path / "jobs.db")])
    assert raised.value.code == 2
