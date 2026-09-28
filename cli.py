"""AGENTIC STAR Marketplace entry adapter for CMN-C2-655."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import current_secrets
from shared.services.llm.base_llm import BaseLLM

try:
    from framework.utils.config_loader import load_agent_config, load_config
except ImportError:  # AgentCore 1.0.0 / 1.0.1 compatibility
    from framework.utils.config_loader import load_config

    def load_agent_config(agent_dir: Path) -> dict[str, Any]:
        config_path = agent_dir / "config" / "config.yaml"
        return load_config(str(config_path)) if config_path.exists() else {}


try:
    from shared.bootstrap import run_agent_marketplace
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.bootstrap":
        raise
    _BOOTSTRAP_IMPORT_ERROR: ModuleNotFoundError | None = exc

    def run_agent_marketplace(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise RuntimeError(
            "The installed AgentCore wheel does not provide Marketplace bootstrap support"
        ) from _BOOTSTRAP_IMPORT_ERROR
else:
    _BOOTSTRAP_IMPORT_ERROR = None


try:
    from shared.secrets.env_provider import EnvProvider
except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
    if exc.name != "shared.secrets.env_provider":
        raise

    class EnvProvider(SecretProvider):  # type: ignore[no-redef]
        """Minimal process-environment provider for AgentCore 1.0.1."""

        def __init__(self, namespace: str = "", agent_name: str = "") -> None:
            self._namespace = namespace
            self._agent_name = agent_name

        def get(self, key: str, default: str | None = None) -> str | None:
            return os.environ.get(key, default)


from src.graph.graph import CorporateGroupTripPlannerGraph
from src.services.trip_repository_factory import create_trip_repositories

_NAMESPACE = "cmn"
_AGENT_NAME = "CMN-C2-655"
_PROVIDER_AGENT_NAME = "cmn-c2-655"

_AZURE_OPENAI_SECRET_KEYS = (
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_DEPLOYMENT",
)
_TRIP_DB_SECRET = "TRIP_DB_URL"
_REQUIRED_SECRET_KEYS = (*_AZURE_OPENAI_SECRET_KEYS, _TRIP_DB_SECRET)

_REQUEST_PARAM_KEYS = frozenset(
    {
        "party_size",
        "budget_band",
        "season",
        "departure_point",
        "duration_nights",
        "destination_hint",
        "org_unit",
    }
)


class _AllowlistedEnvironmentProvider(EnvProvider):  # type: ignore[misc]
    """Expose only the secrets consumed by this single-agent adapter."""

    def __init__(self) -> None:
        super().__init__(namespace=_NAMESPACE, agent_name=_PROVIDER_AGENT_NAME)

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in _REQUIRED_SECRET_KEYS:
            return default
        value = super().get(key)
        return value if value is not None else default


def _require_clean(provider: SecretProvider, key: str) -> str:
    """Resolve a required deployment value without repairing it."""

    value = provider.require(key)
    if not value or any(char.isspace() for char in value):
        raise ValueError(f"Marketplace environment variable {key!r} must be a clean non-empty string")
    return value


def _require_azure_endpoint(provider: SecretProvider) -> str:
    """Reject ``AZURE_OPENAI_ENDPOINT`` values that are not a bare Azure resource endpoint.

    ``AzureOpenAIClient`` appends its own ``/openai/v1/`` path, so anything
    beyond a scheme and host silently produces a broken request URL, and the
    failure surfaces inside ``complete()``. A node that swallows that exception
    degrades quietly while the invocation still reports ``status: success``,
    which is why this has to be a startup check. The value is reported here,
    never repaired.
    """

    endpoint = _require_clean(provider, "AZURE_OPENAI_ENDPOINT")
    parts = urlsplit(endpoint)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == -1
        or parts.path.strip("/")
        or parts.query
        or parts.fragment
        or any(character.isspace() for character in endpoint)
    ):
        raise ValueError(
            "Marketplace environment variable 'AZURE_OPENAI_ENDPOINT' must be a bare Azure "
            "resource endpoint -- scheme and host only, with no path (such as "
            "'/openai'), query or fragment"
        )
    return endpoint


class _AzureOpenAIClientCompat(BaseLLM):
    """Small Azure chat-completion client for AgentCore 1.0.1."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = dict(config)

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        endpoint = self._config["azure_endpoint"].rstrip("/")
        deployment = quote(self._config["azure_deployment"], safe="")
        request = Request(
            f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version=2024-10-21",
            data=json.dumps({"messages": messages}).encode("utf-8"),
            headers={"api-key": self._config["api_key"], "content-type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=30) as response:  # noqa: S310 - deployment-owned endpoint
            payload = json.loads(response.read().decode("utf-8"))
        choices = payload.get("choices", []) if isinstance(payload, dict) else []
        message = choices[0].get("message", {}) if choices else {}
        content = message.get("content", "") if isinstance(message, dict) else ""
        return {
            "content": content,
            "tool_calls": message.get("tool_calls", []) if isinstance(message, dict) else [],
            "model": payload.get("model", self._config["azure_deployment"]),
            "usage": payload.get("usage", {}),
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        content = self.complete(messages).get("content", "")
        if content:
            yield str(content)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        if tools:
            raise NotImplementedError("Azure compatibility client does not support tool binding")
        return self


class _AzureMarketplaceLLM(BaseLLM):
    """Resolve Azure credentials and construct a fresh client per operation."""

    @staticmethod
    def _new_client() -> BaseLLM:
        provider = current_secrets()
        values = {key: _require_clean(provider, key) for key in _AZURE_OPENAI_SECRET_KEYS}

        try:
            from shared.services.llm.azure_openai_client import AzureOpenAIClient
        except ModuleNotFoundError as exc:  # AgentCore 1.0.0 / 1.0.1 compatibility
            if exc.name != "shared.services.llm.azure_openai_client":
                raise
            AzureOpenAIClient = _AzureOpenAIClientCompat  # type: ignore[misc,assignment]

        return cast(
            BaseLLM,
            AzureOpenAIClient(
                {
                    "api_key": values["AZURE_OPENAI_API_KEY"],
                    "azure_endpoint": values["AZURE_OPENAI_ENDPOINT"],
                    "azure_deployment": values["AZURE_OPENAI_DEPLOYMENT"],
                }
            ),
        )

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self._new_client().complete(messages))

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield from self._new_client().stream(messages)

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        return self._new_client().bind_tools(tools)


def _marketplace_inputs(user_input: str) -> tuple[str, dict[str, Any]]:
    """Normalize JSON text and project it into a read-only plan context."""

    try:
        request = json.loads(user_input)
    except (json.JSONDecodeError, TypeError):
        request: Any = None
    if not isinstance(request, dict):
        return user_input, {"mode": "plan", "request_params": {}, "care_notes": ""}

    raw_params = request.get("request_params")
    request_params = (
        {key: value for key, value in raw_params.items() if key in _REQUEST_PARAM_KEYS}
        if isinstance(raw_params, dict)
        else {}
    )
    care_notes = request.get("care_notes")
    return (
        json.dumps(request, ensure_ascii=False),
        {
            "mode": "plan",
            "request_params": request_params,
            "care_notes": care_notes if isinstance(care_notes, str) else "",
        },
    )


class MarketplaceGraph(CorporateGroupTripPlannerGraph):
    """Marketplace-only read boundary with Azure and SQLite wiring."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        merged = dict(config or {})
        provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_SECRET_KEYS:
            _require_clean(provider, key)

        repositories = create_trip_repositories(_require_clean(provider, _TRIP_DB_SECRET))
        merged["policy_db"] = repositories.policy_database
        merged["trip_records_db"] = repositories.trip_records_database

        llm_cfg = dict(merged.get("llm") or {})
        if llm_cfg.get("instance") is None:
            llm_cfg["instance"] = _AzureMarketplaceLLM()
        merged["llm"] = llm_cfg

        super().__init__(config=merged)

    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: InvocationContext | None = None,
        input_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del input_context
        marketplace_user_input, marketplace_context = _marketplace_inputs(user_input)
        if ctx is None:
            ctx = InvocationContext(
                session_id=session_id,
                caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
                hitl_allowed=False,
            )
        else:
            ctx = replace(ctx, hitl_allowed=False)
        return cast(
            dict[str, Any],
            super().invoke(
                marketplace_user_input,
                session_id=session_id,
                ctx=ctx,
                input_context=marketplace_context,
            ),
        )

    def provision_secrets(self, provider: SecretProvider) -> None:
        del provider
        environment_provider = _AllowlistedEnvironmentProvider()
        for key in _REQUIRED_SECRET_KEYS:
            _require_clean(environment_provider, key)
        _require_azure_endpoint(environment_provider)
        super().provision_secrets(environment_provider)


def main() -> None:
    agent_dir = Path(__file__).resolve().parent
    config = load_agent_config(agent_dir)
    if not config:
        config = load_config(str(agent_dir / "config" / "config.yaml"))
    run_agent_marketplace(
        MarketplaceGraph,
        agent_name=_AGENT_NAME,
        namespace=_NAMESPACE,
        config=config,
    )


if __name__ == "__main__":
    main()
