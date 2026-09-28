"""Marketplace entry adapter tests for CMN-C2-655."""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_agent_config
from langgraph.types import Interrupt
from langgraph.checkpoint.memory import InMemorySaver
from shared.services.llm.base_llm import BaseLLM

import cli

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SECRET_VALUES = {
    "AZURE_OPENAI_API_KEY": "test-key",
    "AZURE_OPENAI_ENDPOINT": "https://example.invalid",
    "AZURE_OPENAI_DEPLOYMENT": "test-deployment",
    "TRIP_DB_URL": ":memory:",
}


class _FakeLLM(BaseLLM):
    def __init__(self) -> None:
        self.complete_calls = 0

    def complete(self, messages: list[Any]) -> dict[str, Any]:
        del messages
        self.complete_calls += 1
        return {
            "content": json.dumps(
                {
                    "plans": [
                        {
                            "destination_area": "Kyoto",
                            "itinerary": ["Day 1: meeting"],
                            "lodging": "Business Hotel",
                            "transport": "Train",
                            "estimated_cost_per_person": 50_000,
                            "rationale": "Within the requested conditions",
                            "care_accommodations": [],
                        }
                    ]
                }
            ),
            "tool_calls": [],
            "model": "fake",
            "usage": {},
        }

    def stream(self, messages: list[Any]) -> Iterator[str]:
        yield self.complete(messages)["content"]

    def bind_tools(self, tools: list[Any]) -> BaseLLM:
        del tools
        return self


class _WideProvider(SecretProvider):
    def get(self, key: str, default: str | None = None) -> str | None:
        return f"runner-{key}" if key else default


@pytest.fixture(autouse=True)
def _marketplace_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key, value in _SECRET_VALUES.items():
        monkeypatch.setenv(key, value)
    # Install the fake through sys.modules rather than importing the real module.
    # `shared.services.llm.azure_openai_client` imports `langchain_openai` at module
    # level, which the CI wheel spec (`[anthropic,platform-rag]`) does not install —
    # importing it here made every test in this file ERROR at setup on CI while
    # passing locally. The adapter itself imports that module lazily inside
    # `_client()`, so a sys.modules stub exercises the exact production path.
    stub = types.ModuleType("shared.services.llm.azure_openai_client")
    stub.AzureOpenAIClient = lambda config: _FakeLLM()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "shared.services.llm.azure_openai_client", stub)
    yield


def _config(*, llm: BaseLLM | None = None) -> dict[str, Any]:
    config = load_agent_config(_REPO_ROOT)
    if llm is not None:
        config["llm"] = {**config["llm"], "instance": llm}
    return config


def _compiled_agent(fake: _FakeLLM | None = None) -> tuple[cli.MarketplaceGraph, _FakeLLM]:
    selected = fake or _FakeLLM()
    agent = cli.MarketplaceGraph(config=_config(llm=selected))
    agent.provision_secrets(cli._AllowlistedEnvironmentProvider())
    agent.compile(checkpointer=InMemorySaver())
    return agent, selected


def _invoke_and_state(
    user_input: str,
    *,
    input_context: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], _FakeLLM]:
    agent, fake = _compiled_agent()
    ctx = InvocationContext(
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=True,
    )
    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(user_input, ctx=ctx, input_context=input_context)
    assert agent._compiled is not None
    snapshot = agent._compiled.get_state(
        {"configurable": {"thread_id": ctx.thread_id}}
    )
    inner = agent._nodes["main"].get_subgraph()
    inner_snapshot = inner._compiled.get_state(inner._thread_config(ctx.thread_id))
    return result, dict(snapshot.values), dict(inner_snapshot.values), fake


def _walk_checkpoint_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
        return
    if value is None or isinstance(value, (bool, int, float)):
        return
    if isinstance(value, Interrupt):
        yield from _walk_checkpoint_strings(value.id)
        yield from _walk_checkpoint_strings(value.value)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_checkpoint_strings(key)
            yield from _walk_checkpoint_strings(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            yield from _walk_checkpoint_strings(item)
        return
    raise AssertionError(f"Unsupported checkpoint type: {type(value).__name__}")


def _all_checkpoint_strings(agent: cli.MarketplaceGraph, thread_id: str) -> list[str]:
    strings: list[str] = []
    config = {"configurable": {"thread_id": thread_id}}
    graphs = [agent, agent._nodes["main"].get_subgraph()]
    for graph in graphs:
        assert graph._compiled is not None
        for checkpoint_tuple in graph._compiled.checkpointer.list(config):
            for component in ("checkpoint", "metadata", "pending_writes"):
                strings.extend(
                    _walk_checkpoint_strings(getattr(checkpoint_tuple, component))
                )
    return strings


@pytest.mark.parametrize(
    "requested_mode,write_node",
    [("ingest", "IngestStoreNode"), ("feedback", "FeedbackUpdateNode")],
)
def test_marketplace_forces_plan_mode_and_never_reaches_write_nodes(
    requested_mode: str,
    write_node: str,
) -> None:
    user_input = json.dumps(
        {
            "mode": requested_mode,
            "ingest_payload": {"rows": [{"secret": "must-not-enter-context"}]},
            "request_params": {"party_size": 4, "destination_hint": "Kyoto"},
        }
    )

    result, state, inner_state, fake = _invoke_and_state(user_input)

    assert result["status"] == "success"
    assert state["input_context"]["mode"] == "plan"
    assert "ingest_payload" not in state["input_context"]
    assert write_node not in inner_state["node_history"]
    assert fake.complete_calls >= 1


def test_marketplace_rebuilds_input_context_and_allowlists_request_fields() -> None:
    allowed = {
        "party_size": 8,
        "budget_band": "standard",
        "season": "autumn",
        "departure_point": "Tokyo",
        "duration_nights": 2,
        "destination_hint": "Kyoto",
        "org_unit": "sales",
    }
    user_input = json.dumps(
        {
            "mode": "ingest",
            "request_params": {**allowed, "unknown": "drop-me"},
            "care_notes": ["not", "a", "string"],
        }
    )

    result, state, _, _ = _invoke_and_state(
        user_input,
        input_context={"conversation_history": ["CANARY"], "mode": "ingest"},
    )

    assert state["input_context"] == {
        "mode": "plan",
        "request_params": allowed,
        "care_notes": "",
        "_care_notes_identifier_redaction_count": 0,
    }
    assert "conversation_history" not in state["input_context"]
    assert "unknown" not in state["input_context"]["request_params"]
    assert "CANARY" not in json.dumps(result["output"], ensure_ascii=False)


def test_non_object_or_invalid_json_gets_safe_plan_defaults() -> None:
    expected = {"mode": "plan", "request_params": {}, "care_notes": ""}
    assert cli._marketplace_inputs("plain text") == ("plain text", expected)
    assert cli._marketplace_inputs("[]") == ("[]", expected)


def test_constructor_preserves_llm_settings_and_injects_nested_instance() -> None:
    agent = cli.MarketplaceGraph(config=_config())

    assert isinstance(agent.config["llm"]["instance"], cli._AzureMarketplaceLLM)
    assert vars(agent.config["llm"]["instance"]) == {}
    assert agent.config["llm"]["model"] == "claude-sonnet"
    assert agent.config["llm"]["temperature"] == 0.2
    assert agent.config["llm"]["max_tokens"] == 4000


def test_constructor_preserves_caller_llm_instance() -> None:
    fake = _FakeLLM()

    agent = cli.MarketplaceGraph(config=_config(llm=fake))

    assert agent.config["llm"]["instance"] is fake


def test_repositories_reach_inner_graph_services() -> None:
    agent = cli.MarketplaceGraph(config=_config())
    agent.compile(checkpointer=InMemorySaver())

    inner = agent._nodes["main"].get_subgraph()
    policy_service = inner._nodes["policy_check"]._policy_service
    records_service = inner._nodes["feedback_update"]._trip_records_service

    assert agent.config["policy_db"] is not None
    assert agent.config["trip_records_db"] is not None
    assert policy_service is not None
    assert records_service is not None
    assert policy_service._db is agent.config["policy_db"]
    assert records_service._db is agent.config["trip_records_db"]


def test_provision_secrets_replaces_runner_provider_with_allowlisted_environment() -> None:
    agent = cli.MarketplaceGraph(config=_config())

    agent.provision_secrets(_WideProvider())

    assert isinstance(agent._secrets_provider, cli._AllowlistedEnvironmentProvider)
    assert agent._secrets_provider.get("AZURE_OPENAI_API_KEY") == "test-key"
    assert agent._secrets_provider.get("UNDECLARED_SECRET") is None


def test_azure_wrapper_maps_secrets_lazily_and_bind_tools_uses_fresh_client() -> None:
    import shared.services.llm.azure_openai_client as azure_module

    clients: list[BaseLLM] = []
    configs: list[dict[str, str]] = []
    original_client = azure_module.AzureOpenAIClient

    class RecordingClient(_FakeLLM):
        def __init__(self, config: dict[str, str]) -> None:
            super().__init__()
            configs.append(config)
            clients.append(self)

    try:
        azure_module.AzureOpenAIClient = RecordingClient
        llm = cli._AzureMarketplaceLLM()
        with bound_secrets(cli._AllowlistedEnvironmentProvider()):
            llm.complete([])
            bound = llm.bind_tools([])
    finally:
        azure_module.AzureOpenAIClient = original_client

    assert vars(llm) == {}
    assert len(clients) == 2
    assert bound is clients[1]
    assert configs == [
        {
            "api_key": "test-key",
            "azure_endpoint": "https://example.invalid",
            "azure_deployment": "test-deployment",
        },
        {
            "api_key": "test-key",
            "azure_endpoint": "https://example.invalid",
            "azure_deployment": "test-deployment",
        },
    ]


def test_main_loads_nonempty_config_and_passes_marketplace_identity() -> None:
    calls: list[tuple[Any, dict[str, Any]]] = []
    loaded_path: list[str] = []
    original_runner = cli.run_agent_marketplace
    original_agent_loader = cli.load_agent_config
    original_config_loader = cli.load_config

    def record_runner(graph_class: Any, **kwargs: Any) -> None:
        calls.append((graph_class, kwargs))

    def empty_agent_config(agent_dir: Path) -> dict[str, Any]:
        assert agent_dir == _REPO_ROOT
        return {}

    def fallback_config(path: str) -> dict[str, Any]:
        loaded_path.append(path)
        return {"llm": {"model": "from-file"}}

    try:
        cli.run_agent_marketplace = record_runner
        cli.load_agent_config = empty_agent_config
        cli.load_config = fallback_config
        cli.main()
    finally:
        cli.run_agent_marketplace = original_runner
        cli.load_agent_config = original_agent_loader
        cli.load_config = original_config_loader

    assert loaded_path == [str(_REPO_ROOT / "config" / "config.yaml")]
    assert calls == [
        (
            cli.MarketplaceGraph,
            {
                "agent_name": "CMN-C2-655",
                "namespace": "cmn",
                "config": {"llm": {"model": "from-file"}},
            },
        )
    ]


@pytest.mark.parametrize("missing_key", cli._REQUIRED_SECRET_KEYS)
@pytest.mark.parametrize("bad_value", [None, "contains whitespace"])
def test_constructor_fails_closed_for_missing_or_unclean_secrets(
    monkeypatch: pytest.MonkeyPatch,
    missing_key: str,
    bad_value: str | None,
) -> None:
    if bad_value is None:
        monkeypatch.delenv(missing_key)
    else:
        monkeypatch.setenv(missing_key, bad_value)

    with pytest.raises(ValueError) as exc_info:
        cli.MarketplaceGraph(config=_config())

    assert missing_key in str(exc_info.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.cognitiveservices.azure.com/openai/responses?api-version=2025-04-01-preview",
        "https://example.services.ai.azure.example/openai",
        "https://example.services.ai.azure.example/v1",
        "https://example.services.ai.azure.example?api-version=2024-10-21",
        "example.services.ai.azure.example",
    ],
    ids=["api-path", "openai-path", "base-path", "query-only", "missing-scheme"],
)
def test_unusable_azure_endpoint_shape_fails_during_startup(
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """AzureOpenAIClient appends its own path, so anything but a bare endpoint breaks.

    The resulting error surfaces inside complete(); a node that swallows it
    degrades silently while the invocation still reports status: success, so the
    adapter has to reject the shape at startup instead.
    """

    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", endpoint)
    agent = cli.MarketplaceGraph(config=_config())

    with pytest.raises(ValueError, match="bare"):
        agent.provision_secrets(cli._AllowlistedEnvironmentProvider())


def test_runner_order_invokes_fake_llm_and_skips_hitl() -> None:
    result, state, _, fake = _invoke_and_state(
        json.dumps(
            {
                "request_params": {
                    "party_size": 6,
                    "budget_band": "standard",
                    "season": "spring",
                    "departure_point": "Tokyo",
                    "destination_hint": "Kyoto",
                }
            }
        )
    )

    assert fake.complete_calls >= 1
    assert result["status"] == "success"
    assert result["output"]
    assert result["output"]["json"]["selection_status"] == "pending_organizer"
    assert state["hitl_allowed"] is False
    assert state["mode"] == "plan"


def test_care_notes_identifiers_do_not_reach_formatted_output() -> None:
    user_input = json.dumps(
        {
            "request_params": {"party_size": 4, "destination_hint": "Kyoto"},
            "care_notes": "田中太郎さんは車椅子、080-1234-5678",
        },
        ensure_ascii=False,
    )

    result, state, _, _ = _invoke_and_state(user_input)
    rendered = json.dumps(result["output"], ensure_ascii=False)
    care_requirements = result["output"]["json"]["care_requirements"]

    assert "田中太郎" not in rendered
    assert "080-1234-5678" not in rendered
    assert care_requirements
    assert all(set(item) == {"category", "count"} for item in care_requirements)
    assert all(isinstance(item["count"], int) for item in care_requirements)
    assert state["hitl_allowed"] is False


def test_ascii_escaped_json_identifiers_are_absent_from_all_checkpoints() -> None:
    agent, _ = _compiled_agent()
    ctx = InvocationContext(
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=True,
    )
    user_input = json.dumps(
        {
            "request_params": {"party_size": 12},
            "care_notes": "田中太郎さんは車椅子、連絡先080-1234-5678",
        }
    )

    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(user_input, ctx=ctx)

    assert result["status"] == "success"
    strings = _all_checkpoint_strings(agent, ctx.thread_id)
    assert strings
    escaped_name = json.dumps("田中太郎")[1:-1]
    for text in strings:
        assert "080-1234-5678" not in text
        assert "田中太郎" not in text
        assert escaped_name not in text


def test_invoke_without_context_still_disables_hitl() -> None:
    agent, fake = _compiled_agent()

    with bound_secrets(agent._secrets_provider):
        result = agent.invoke(
            json.dumps({"request_params": {"party_size": 12}}),
            ctx=None,
        )

    assert result["status"] == "success"
    assert result["status"] != "awaiting_human"
    assert result["output"]["json"]["selection_status"] == "pending_organizer"
    assert fake.complete_calls >= 1
