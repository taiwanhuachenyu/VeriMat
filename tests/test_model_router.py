import json

import pytest

from src.evaluation.claude_code_transport import (
    CLAUDE_CODE_ROUTE_ID, ClaudeCodeStructuredTransport,
)
from src.evaluation.model_router import (
    DEFAULT_ROUTE, OPENCODE_ROUTE_ID, ROUTE_CLAUDE_CODE, ROUTE_OPENCODE, ROUTES,
    RouteConfigurationError, attested_backend_report, credential_fingerprint, open_route,
    opencode_server_environment, resolve_route, route_disclosure,
)
from src.evaluation.opencode_transport import OpenCodeStructuredTransport

SECRET = "sk-live-do-not-print-me-4f9a2c"

CLAUDE_ENV = {"VERIMAT_CLAUDE_CLI": "fake-claude"}

OPENCODE_ENV = {
    "VERIMAT_MODEL_ROUTE": ROUTE_OPENCODE,
    "VERIMAT_OPENCODE_BASE_URL": "http://127.0.0.1:4096",
    "VERIMAT_OPENCODE_PROVIDER": "anthropic",
    "VERIMAT_OPENCODE_MODEL": "claude-sonnet-5",
    "VERIMAT_OPENCODE_API_KEY": SECRET,
}


# --------------------------------------------------------------------------- resolution

def test_the_default_route_is_the_already_authenticated_session():
    assert DEFAULT_ROUTE == ROUTE_CLAUDE_CODE
    assert resolve_route(env={}) == ROUTE_CLAUDE_CODE


def test_an_explicit_route_outranks_the_environment():
    assert resolve_route(ROUTE_CLAUDE_CODE, env={"VERIMAT_MODEL_ROUTE": ROUTE_OPENCODE}) == (
        ROUTE_CLAUDE_CODE
    )


def test_the_environment_selects_the_route_when_no_argument_is_given():
    assert resolve_route(env={"VERIMAT_MODEL_ROUTE": ROUTE_OPENCODE}) == ROUTE_OPENCODE
    assert resolve_route(env={"VERIMAT_MODEL_ROUTE": "  OpenCode "}) == ROUTE_OPENCODE


def test_an_unknown_route_names_the_valid_choices():
    with pytest.raises(RouteConfigurationError, match="unknown model route"):
        resolve_route("gpt-please", env={})


# --------------------------------------------------------------------------- fail closed

def test_the_opencode_route_refuses_to_start_without_its_api_key(tmp_path):
    env = dict(OPENCODE_ENV)
    env.pop("VERIMAT_OPENCODE_API_KEY")
    with pytest.raises(RouteConfigurationError, match="VERIMAT_OPENCODE_API_KEY"):
        open_route(env=env, operation_db=tmp_path / "operations.db")


def test_every_missing_opencode_variable_is_reported_at_once(tmp_path):
    with pytest.raises(RouteConfigurationError) as raised:
        open_route(ROUTE_OPENCODE, env={}, operation_db=tmp_path / "operations.db")
    message = str(raised.value)
    for name in ("PROVIDER", "MODEL", "API_KEY"):
        assert f"VERIMAT_OPENCODE_{name}" in message


def test_the_claude_code_route_needs_no_api_key(tmp_path):
    with open_route(env=CLAUDE_ENV, operation_db=tmp_path / "operations.db") as selection:
        assert selection.route == ROUTE_CLAUDE_CODE
        assert isinstance(selection.transport, ClaudeCodeStructuredTransport)
        assert selection.disclosure["credential_fingerprint"] is None


# --------------------------------------------------------------------------- construction

def test_each_route_builds_its_own_transport(tmp_path):
    with open_route(env=CLAUDE_ENV, operation_db=tmp_path / "claude.db") as claude:
        assert claude.provenance.route_id == CLAUDE_CODE_ROUTE_ID
        assert claude.provenance.request_alias == "session-default"
    with open_route(env=OPENCODE_ENV, operation_db=tmp_path / "opencode.db") as opencode:
        assert isinstance(opencode.transport, OpenCodeStructuredTransport)
        assert opencode.provenance.route_id == OPENCODE_ROUTE_ID
        assert opencode.provenance.request_alias == "anthropic/claude-sonnet-5"


def test_both_routes_satisfy_the_same_backend_contract(tmp_path):
    for name, env in ((ROUTE_CLAUDE_CODE, CLAUDE_ENV), (ROUTE_OPENCODE, OPENCODE_ENV)):
        with open_route(env=env, operation_db=tmp_path / f"{name}.db") as selection:
            backend = selection.backend()
            assert backend.provider_id == selection.provenance.route_id
            assert callable(selection.transport.complete)


def test_the_configured_model_alias_is_honoured(tmp_path):
    env = dict(CLAUDE_ENV, VERIMAT_CLAUDE_CODE_MODEL="claude-opus-5")
    with open_route(env=env, operation_db=tmp_path / "operations.db") as selection:
        assert selection.transport.model == "claude-opus-5"
        assert selection.provenance.request_alias == "claude-opus-5"


def test_opencode_rejects_a_usage_log_it_cannot_write(tmp_path):
    with pytest.raises(RouteConfigurationError, match="accepts no usage_log"):
        open_route(
            env=OPENCODE_ENV, operation_db=tmp_path / "operations.db",
            usage_log=tmp_path / "usage.jsonl",
        )


def test_a_route_selection_closes_its_transport(tmp_path):
    selection = open_route(env=CLAUDE_ENV, operation_db=tmp_path / "operations.db")
    selection.close()
    with pytest.raises(Exception):
        selection.transport.conn.execute("SELECT 1")


# --------------------------------------------------------------------------- disclosure

@pytest.mark.parametrize("route", ROUTES)
def test_every_route_discloses_the_five_required_dependency_facts(route):
    env = OPENCODE_ENV if route == ROUTE_OPENCODE else {}
    disclosure = route_disclosure(route, env=env)
    required = (
        "invocation_points", "cost_assumptions", "permission_scope",
        "substitutability", "migration_cost",
    )
    for field in required:
        assert disclosure[field], f"{route} disclosed nothing for {field}"
    assert set(disclosure["field_labels_zh"]) == set(required)
    assert disclosure["route"] == route


@pytest.mark.parametrize("route", ROUTES)
def test_each_route_names_the_other_as_its_substitute(route):
    env = OPENCODE_ENV if route == ROUTE_OPENCODE else {}
    other = ROUTE_CLAUDE_CODE if route == ROUTE_OPENCODE else ROUTE_OPENCODE
    text = json.dumps(route_disclosure(route, env=env), ensure_ascii=False)
    assert other in text


def test_the_disclosure_is_json_serialisable_for_the_run_manifest(tmp_path):
    with open_route(env=OPENCODE_ENV, operation_db=tmp_path / "operations.db") as selection:
        manifest = selection.manifest()
        assert json.loads(json.dumps(manifest, ensure_ascii=False))
        assert manifest["provenance"]["route_id"] == OPENCODE_ROUTE_ID
        assert manifest["provenance"]["backend_independently_attested"] is False


# --------------------------------------------------------------------------- secret handling

def test_the_api_key_never_appears_in_the_disclosure(tmp_path):
    disclosure = route_disclosure(ROUTE_OPENCODE, env=OPENCODE_ENV)
    assert SECRET not in json.dumps(disclosure, ensure_ascii=False)
    assert disclosure["credential_fingerprint"] == credential_fingerprint(SECRET)


def test_the_api_key_never_appears_anywhere_in_the_run_manifest(tmp_path):
    with open_route(env=OPENCODE_ENV, operation_db=tmp_path / "operations.db") as selection:
        assert SECRET not in json.dumps(selection.manifest(), ensure_ascii=False)


def test_the_fingerprint_identifies_a_key_without_revealing_it():
    fingerprint = credential_fingerprint(SECRET)
    assert len(fingerprint) == 12
    assert SECRET not in fingerprint
    assert fingerprint != credential_fingerprint(SECRET + "x")
    assert fingerprint == credential_fingerprint(SECRET)


def test_the_key_is_handed_only_to_the_server_that_needs_it():
    exported = opencode_server_environment(env=OPENCODE_ENV)
    assert exported == {"ANTHROPIC_API_KEY": SECRET}


def test_an_unmapped_provider_still_gets_a_conventional_variable_name():
    env = dict(OPENCODE_ENV, VERIMAT_OPENCODE_PROVIDER="my-vendor")
    assert opencode_server_environment(env=env) == {"MY_VENDOR_API_KEY": SECRET}


def test_the_server_environment_refuses_to_invent_a_missing_key():
    env = dict(OPENCODE_ENV)
    env.pop("VERIMAT_OPENCODE_API_KEY")
    with pytest.raises(RouteConfigurationError, match="VERIMAT_OPENCODE_API_KEY"):
        opencode_server_environment(env=env)


# --------------------------------------------------------------------------- attestation

def test_an_unused_route_reports_no_provider_evidence_either_way(tmp_path):
    with open_route(env=CLAUDE_ENV, operation_db=tmp_path / "operations.db") as selection:
        report = attested_backend_report(selection)
        assert report["provider_reported_backends"] == []
        assert report["declaration_consistent_with_provider"] is None
        assert report["independently_attested"] is False


def test_a_declared_backend_is_checked_against_what_the_provider_reported(tmp_path):
    env = dict(CLAUDE_ENV, VERIMAT_CLAUDE_CODE_MODEL="claude-opus-5")
    with open_route(env=env, operation_db=tmp_path / "operations.db") as selection:
        selection.transport.observed_backends.add("claude-opus-5")
        assert attested_backend_report(selection)["declaration_consistent_with_provider"]


def test_a_contradicted_declaration_is_reported_rather_than_hidden(tmp_path):
    env = dict(CLAUDE_ENV, VERIMAT_CLAUDE_CODE_MODEL="claude-opus-5")
    with open_route(env=env, operation_db=tmp_path / "operations.db") as selection:
        selection.transport.observed_backends.add("claude-haiku-4-5-20251001")
        report = attested_backend_report(selection)
        assert report["declaration_consistent_with_provider"] is False
        assert report["provider_reported_backends"] == ["claude-haiku-4-5-20251001"]
