"""Choose the model route for a run and state, in machine-readable form, what that costs.

Two routes are available and they differ in *who pays and with whose credential*:

``claude-code``
    Delegates to the Claude Code CLI already authenticated on this machine.  No API key is
    read, because the credential is the operator's running session.

``opencode``
    Delegates to a local OpenCode server that in turn calls a commercial provider with an
    API key.  The key is consumed by the OpenCode *server process*, not by the HTTP client in
    ``opencode_transport``, so this module cannot inject it.  What it can do is refuse to start
    a run whose credential is absent, and record which credential was in force without ever
    writing the secret down -- see ``opencode_server_environment`` for the export the operator
    hands to the server.

Neither transport is modified to fit the other.  The route is resolved once, up front, and the
resulting ``RouteSelection`` carries the disclosure that the dependency statement has to quote.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .baseline_runner import BaselineContractError
from .claude_code_transport import CLAUDE_CODE_ROUTE_ID, ClaudeCodeStructuredTransport
from .model_backend import (
    ProviderProvenance, StructuredModelBackend, StructuredModelTransport,
)
from .opencode_transport import OpenCodeStructuredTransport

ROUTE_CLAUDE_CODE = "claude-code"
ROUTE_OPENCODE = "opencode"
ROUTES = (ROUTE_CLAUDE_CODE, ROUTE_OPENCODE)

OPENCODE_ROUTE_ID = "opencode-local"
DEFAULT_ROUTE = ROUTE_CLAUDE_CODE

ROUTE_ENV = "VERIMAT_MODEL_ROUTE"
USAGE_LOG_ENV = "VERIMAT_MODEL_USAGE_LOG"
CLAUDE_CLI_ENV = "VERIMAT_CLAUDE_CLI"
CLAUDE_MODEL_ENV = "VERIMAT_CLAUDE_CODE_MODEL"
OPENCODE_BASE_URL_ENV = "VERIMAT_OPENCODE_BASE_URL"
OPENCODE_PROVIDER_ENV = "VERIMAT_OPENCODE_PROVIDER"
OPENCODE_MODEL_ENV = "VERIMAT_OPENCODE_MODEL"
OPENCODE_AGENT_ENV = "VERIMAT_OPENCODE_AGENT"
OPENCODE_API_KEY_ENV = "VERIMAT_OPENCODE_API_KEY"

DEFAULT_OPENCODE_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_OPENCODE_AGENT = "benchmark"

SESSION_DEFAULT_ALIAS = "session-default"


class RouteConfigurationError(BaselineContractError):
    """The requested route cannot be configured, so no call is attempted."""


def _text(env: Mapping[str, str], name: str) -> str:
    return (env.get(name) or "").strip()


def credential_fingerprint(secret: str) -> str:
    """Identify a credential in the audit record without disclosing it.

    Truncated to 12 hex characters: enough to tell two keys apart across runs, far too little
    to help an attacker who has the digest recover the key.
    """
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()[:12]


def resolve_route(explicit: str | None = None, *, env: Mapping[str, str] | None = None) -> str:
    """Resolve the route from the explicit argument, then the environment, then the default."""
    environment = os.environ if env is None else env
    candidate = (explicit or _text(environment, ROUTE_ENV) or DEFAULT_ROUTE).strip().lower()
    if candidate not in ROUTES:
        raise RouteConfigurationError(
            f"unknown model route {candidate!r}; choose one of {', '.join(ROUTES)}"
        )
    return candidate


# --------------------------------------------------------------------------- disclosure

_COMMON_INVOCATION_POINTS = (
    "query planning (StructuredModelBackend.plan_queries)",
    "evidence decision (StructuredModelBackend.decide)",
)

_DISCLOSURE_LABELS_ZH = {
    "invocation_points": "调用环节",
    "cost_assumptions": "费用假设",
    "permission_scope": "权限范围",
    "substitutability": "可替代性",
    "migration_cost": "迁移成本",
}


def route_disclosure(
    route: str, *, env: Mapping[str, str] | None = None, model_alias: str | None = None,
) -> dict[str, Any]:
    """The commercial-dependency statement for one route, ready to embed in a run manifest.

    The five fields are the ones a reviewer asks about a paid dependency: where it is called,
    what it costs, what it is permitted to touch, what replaces it, and what replacing it costs.
    """
    environment = os.environ if env is None else env
    route = resolve_route(route, env=environment)
    if route == ROUTE_CLAUDE_CODE:
        alias = model_alias or _text(environment, CLAUDE_MODEL_ENV) or SESSION_DEFAULT_ALIAS
        body: dict[str, Any] = {
            "route_id": CLAUDE_CODE_ROUTE_ID,
            "vendor": "Anthropic Claude Code CLI",
            "credential": "the operator's already-authenticated CLI session; no API key is read",
            "credential_fingerprint": None,
            "request_alias": alias,
            "invocation_points": list(_COMMON_INVOCATION_POINTS),
            "cost_assumptions": [
                "billed per call to the operator's existing Claude subscription or API account",
                "the CLI reports authoritative per-call usage and total_cost_usd, which is"
                " recorded verbatim in the usage log rather than estimated",
                "the base system prompt is cached: the first call in a cache window pays"
                " cache-creation rates and later identical-prefix calls pay cache-read rates,"
                " so per-call cost is not constant",
                "a repeated operation_id is served from the local SQLite cache and costs nothing",
            ],
            "permission_scope": [
                "one non-interactive turn per operation, prompt delivered on stdin",
                "every built-in tool is passed to --disallowedTools, and a call that still"
                " requests a permission is failed rather than answered",
                "--exclude-dynamic-system-prompt-sections removes working directory, date and"
                " git state from the prompt, so nothing about the repository leaves the machine"
                " except the prompt the caller composed",
            ],
            "substitutability": [
                f"the {ROUTE_OPENCODE} route serves the same StructuredModelTransport protocol",
                "any transport implementing complete(operation_id, system, user,"
                " response_schema) can replace it without touching call sites",
            ],
            "migration_cost": [
                f"set {ROUTE_ENV}={ROUTE_OPENCODE} and supply the OpenCode variables",
                "no change to search, evidence or reporting code; the operation cache is shared,"
                " so completed operations are not re-billed after a switch",
            ],
        }
    else:
        key = _text(environment, OPENCODE_API_KEY_ENV)
        provider = _text(environment, OPENCODE_PROVIDER_ENV) or "unset"
        model = _text(environment, OPENCODE_MODEL_ENV) or "unset"
        body = {
            "route_id": OPENCODE_ROUTE_ID,
            "vendor": f"local OpenCode server proxying provider {provider!r}",
            "credential": (
                f"API key supplied through {OPENCODE_API_KEY_ENV} and consumed by the OpenCode"
                " server process; never sent by this client and never written to any log"
            ),
            "credential_fingerprint": credential_fingerprint(key) if key else None,
            "request_alias": f"{provider}/{model}",
            "invocation_points": list(_COMMON_INVOCATION_POINTS),
            "cost_assumptions": [
                "billed per call by the upstream provider that OpenCode is configured against",
                "OpenCode reports input and output token counts, which are recorded per"
                " operation; it reports no monetary total, so cost must be derived from the"
                " provider's published rate for the configured model",
                "a repeated operation_id is served from the local SQLite cache and costs nothing",
            ],
            "permission_scope": [
                "the server must be bound to loopback; the transport refuses any other host",
                "every OpenCode tool is explicitly disabled for the benchmark agent",
                "the API key is held by the server process, so this repository never stores it",
            ],
            "substitutability": [
                f"the {ROUTE_CLAUDE_CODE} route serves the same StructuredModelTransport"
                " protocol",
                "the local server can be pointed at a different provider without code changes",
            ],
            "migration_cost": [
                f"set {ROUTE_ENV}={ROUTE_CLAUDE_CODE} and authenticate the Claude Code CLI",
                "no change to search, evidence or reporting code; the operation cache is shared,"
                " so completed operations are not re-billed after a switch",
            ],
        }
    body["route"] = route
    body["field_labels_zh"] = dict(_DISCLOSURE_LABELS_ZH)
    return body


def opencode_server_environment(*, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment to export when launching the OpenCode server.

    Returned rather than applied: the key belongs to the server process, and this module will
    not mutate the parent environment or hand the secret to anything that logs.
    """
    environment = os.environ if env is None else env
    key = _text(environment, OPENCODE_API_KEY_ENV)
    if not key:
        raise RouteConfigurationError(
            f"the {ROUTE_OPENCODE} route requires {OPENCODE_API_KEY_ENV} to be set"
        )
    provider = _text(environment, OPENCODE_PROVIDER_ENV)
    if not provider:
        raise RouteConfigurationError(
            f"the {ROUTE_OPENCODE} route requires {OPENCODE_PROVIDER_ENV} to be set"
        )
    variable = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openai": "OPENAI_API_KEY",
        "google": "GEMINI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "moonshot": "MOONSHOT_API_KEY",
    }.get(provider.lower(), f"{provider.upper().replace('-', '_')}_API_KEY")
    return {variable: key}


# --------------------------------------------------------------------------- construction

@dataclass
class RouteSelection:
    """A configured route: the transport, its provenance, and its cost disclosure."""

    route: str
    transport: StructuredModelTransport
    provenance: ProviderProvenance
    disclosure: dict[str, Any]

    def backend(self, **kwargs: Any) -> StructuredModelBackend:
        return StructuredModelBackend(
            transport=self.transport, provenance=self.provenance, **kwargs,
        )

    def manifest(self) -> dict[str, Any]:
        return {"provenance": self.provenance.manifest(), "disclosure": self.disclosure}

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "RouteSelection":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def _claude_code_selection(
    *, operation_db: str | Path, env: Mapping[str, str],
    operator_declared_backend: str | None, usage_log: str | Path | None,
    transport_kwargs: Mapping[str, Any],
) -> RouteSelection:
    alias = _text(env, CLAUDE_MODEL_ENV) or None
    transport = ClaudeCodeStructuredTransport(
        operation_db=operation_db,
        cli_path=_text(env, CLAUDE_CLI_ENV) or None,
        model=alias,
        usage_log=usage_log if usage_log is not None else (_text(env, USAGE_LOG_ENV) or None),
        **dict(transport_kwargs),
    )
    provenance = ProviderProvenance(
        route_id=CLAUDE_CODE_ROUTE_ID,
        request_alias=alias or SESSION_DEFAULT_ALIAS,
        operator_declared_backend=operator_declared_backend or alias or SESSION_DEFAULT_ALIAS,
        backend_independently_attested=False,
    )
    return RouteSelection(
        route=ROUTE_CLAUDE_CODE, transport=transport, provenance=provenance,
        disclosure=route_disclosure(ROUTE_CLAUDE_CODE, env=env, model_alias=alias),
    )


def _opencode_selection(
    *, operation_db: str | Path, env: Mapping[str, str],
    operator_declared_backend: str | None, transport_kwargs: Mapping[str, Any],
) -> RouteSelection:
    provider = _text(env, OPENCODE_PROVIDER_ENV)
    model = _text(env, OPENCODE_MODEL_ENV)
    missing = [
        name for name, value in (
            (OPENCODE_PROVIDER_ENV, provider),
            (OPENCODE_MODEL_ENV, model),
            (OPENCODE_API_KEY_ENV, _text(env, OPENCODE_API_KEY_ENV)),
        ) if not value
    ]
    if missing:
        # Fail before the first call: a run that dies part way through has already been charged.
        raise RouteConfigurationError(
            f"the {ROUTE_OPENCODE} route requires {', '.join(missing)} to be set"
        )
    transport = OpenCodeStructuredTransport(
        base_url=_text(env, OPENCODE_BASE_URL_ENV) or DEFAULT_OPENCODE_BASE_URL,
        provider_id=provider,
        model_id=model,
        operation_db=operation_db,
        agent=_text(env, OPENCODE_AGENT_ENV) or DEFAULT_OPENCODE_AGENT,
        **dict(transport_kwargs),
    )
    provenance = ProviderProvenance(
        route_id=OPENCODE_ROUTE_ID,
        request_alias=f"{provider}/{model}",
        operator_declared_backend=operator_declared_backend or f"{provider}/{model}",
        backend_independently_attested=False,
    )
    return RouteSelection(
        route=ROUTE_OPENCODE, transport=transport, provenance=provenance,
        disclosure=route_disclosure(ROUTE_OPENCODE, env=env),
    )


def open_route(
    route: str | None = None, *, operation_db: str | Path,
    env: Mapping[str, str] | None = None,
    operator_declared_backend: str | None = None,
    usage_log: str | Path | None = None,
    **transport_kwargs: Any,
) -> RouteSelection:
    """Configure the selected route, or refuse before any billable call is made."""
    environment = os.environ if env is None else env
    selected = resolve_route(route, env=environment)
    if selected == ROUTE_CLAUDE_CODE:
        return _claude_code_selection(
            operation_db=operation_db, env=environment,
            operator_declared_backend=operator_declared_backend,
            usage_log=usage_log, transport_kwargs=transport_kwargs,
        )
    if usage_log is not None:
        raise RouteConfigurationError(
            f"the {ROUTE_OPENCODE} transport records usage in its own operation table and"
            " accepts no usage_log"
        )
    unsupported = sorted(set(transport_kwargs) - {"timeout_seconds"})
    if unsupported:
        raise RouteConfigurationError(
            f"the {ROUTE_OPENCODE} transport does not accept: {', '.join(unsupported)}"
        )
    return _opencode_selection(
        operation_db=operation_db, env=environment,
        operator_declared_backend=operator_declared_backend,
        transport_kwargs=transport_kwargs,
    )


def attested_backend_report(selection: RouteSelection) -> dict[str, Any]:
    """Compare what the operator declared against what the provider said it served.

    Only the Claude Code CLI names the model that answered, so this is the one route where a
    declared backend can be contradicted by evidence.  A mismatch is reported rather than
    raised: the run already happened, and hiding the discrepancy would be the real failure.
    """
    observed = sorted(getattr(selection.transport, "observed_backends", ()) or ())
    declared = selection.provenance.operator_declared_backend
    consistent = (
        None if not observed
        else (declared in observed or declared == SESSION_DEFAULT_ALIAS)
    )
    return {
        "route": selection.route,
        "operator_declared_backend": declared,
        "provider_reported_backends": observed,
        "declaration_consistent_with_provider": consistent,
        "independently_attested": False,
    }
