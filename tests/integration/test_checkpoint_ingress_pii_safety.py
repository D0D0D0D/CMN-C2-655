"""Regression coverage for PII removal before each public graph ingress."""

from collections.abc import Iterator
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Interrupt
import pytest

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from src.graph.graph import CorporateGroupTripPlannerGraph
from src.services.ingress_sanitizer import CARE_NOTES_REDACTION_COUNT_KEY
import src.nodes.pre_process_node as pre_process_module


def _build_agent() -> CorporateGroupTripPlannerGraph:
    agent = CorporateGroupTripPlannerGraph(
        config={"memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 24}}
    )
    agent.compile(checkpointer=InMemorySaver())
    return agent


def _walk_checkpoint_strings(value: Any, path: str) -> Iterator[str]:
    """Inspect every persisted string without stringifying unknown objects."""
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, (int, float, bool)) or value is None:
        return
    if isinstance(value, Interrupt):
        yield from _walk_checkpoint_strings(value.id, f"{path}.id")
        yield from _walk_checkpoint_strings(value.value, f"{path}.value")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_checkpoint_strings(key, f"{path}.key")
            yield from _walk_checkpoint_strings(item, f"{path}[key]")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _walk_checkpoint_strings(item, f"{path}[{index}]")
        return
    raise AssertionError(f"Unsupported checkpoint type at {path}: {type(value).__name__}")


def _checkpoint_strings(agent: CorporateGroupTripPlannerGraph, thread_id: str) -> list[str]:
    strings: list[str] = []
    outer_config = {"configurable": {"thread_id": thread_id}}
    for cp_tuple in agent._compiled.checkpointer.list(outer_config):
        for component in ("checkpoint", "metadata", "pending_writes"):
            strings.extend(
                _walk_checkpoint_strings(
                    getattr(cp_tuple, component), f"outer.{component}"
                )
            )

    # The inner graph has a dedicated checkpointer. Keep the outer snapshot
    # thread-id resolution used by PB-5 because it is the stable HITL mapping.
    snap = agent._compiled.get_state(outer_config)
    inner_thread_id = snap.values.get("subgraph_thread_id")
    if inner_thread_id:
        inner_graph = agent._nodes["main"].get_subgraph()
        inner_config = {"configurable": {"thread_id": inner_thread_id}}
        for cp_tuple in inner_graph._compiled.checkpointer.list(inner_config):
            for component in ("checkpoint", "metadata", "pending_writes"):
                strings.extend(
                    _walk_checkpoint_strings(
                        getattr(cp_tuple, component), f"inner.{component}"
                    )
                )
    return strings


def _assert_absent(agent: CorporateGroupTripPlannerGraph, thread_id: str, *identifiers: str) -> None:
    strings = _checkpoint_strings(agent, thread_id)
    assert strings, "expected persisted checkpoint content"
    for identifier in identifiers:
        assert all(identifier not in text for text in strings)


class TestCheckpointIngressPiiSafety:
    def test_invoke_sanitizes_user_input_request_params_and_care_notes(self, monkeypatch):
        agent = _build_agent()
        events: list[tuple[str, dict]] = []
        original_emit = pre_process_module.emit_trace_event

        def spy_emit(event_type: str, payload: dict, state: dict) -> None:
            events.append((event_type, payload))
            original_emit(event_type, payload, state)

        monkeypatch.setattr(pre_process_module, "emit_trace_event", spy_emit)
        ctx = InvocationContext(
            thread_id="checkpoint-ingress-invoke",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        result = agent.invoke(
            "田中太郎さんと tanaka@example.com を含む団体旅行",
            ctx=ctx,
            input_context={
                "mode": "plan",
                "request_params": {
                    "party_size": 4,
                    "destination_hint": "Kyoto",
                    "田中様": "同行者",
                },
                "care_notes": "田中太郎さんは vegetarian、連絡先 tanaka@example.com",
            },
        )
        assert result["status"] == "awaiting_human"
        _assert_absent(agent, "checkpoint-ingress-invoke", "田中太郎", "tanaka@example.com", "田中様")
        care_events = [payload for event_type, payload in events if event_type == "care_notes_identifier_stripped"]
        assert care_events == [{"finding_count": 2}]

    def test_invoke_drops_spoofed_care_notes_count_when_care_notes_are_absent(self, monkeypatch):
        """ATK2: an ingress-only audit key cannot create a false S-4 event."""
        agent = _build_agent()
        events: list[tuple[str, dict]] = []
        original_emit = pre_process_module.emit_trace_event

        def spy_emit(event_type: str, payload: dict, state: dict) -> None:
            events.append((event_type, payload))
            original_emit(event_type, payload, state)

        monkeypatch.setattr(pre_process_module, "emit_trace_event", spy_emit)
        ctx = InvocationContext(
            thread_id="checkpoint-ingress-spoofed-care-count",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        result = agent.invoke(
            "4名 Kyoto",
            ctx=ctx,
            input_context={
                "mode": "plan",
                "request_params": {"party_size": 4},
                CARE_NOTES_REDACTION_COUNT_KEY: 99,
            },
        )

        assert result["status"] == "awaiting_human"
        care_events = [
            payload
            for event_type, payload in events
            if event_type == "care_notes_identifier_stripped"
        ]
        assert care_events == []

    def test_invoke_resume_sanitizes_feedback_before_pending_write(self):
        agent = _build_agent()
        ctx = InvocationContext(
            thread_id="checkpoint-ingress-resume",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        first = agent.invoke(
            "4名 Kyoto",
            ctx=ctx,
            input_context={"mode": "plan", "request_params": {"party_size": 4}},
        )
        assert first["status"] == "awaiting_human"

        resumed = agent.resume(
            first["thread_id"],
            {"田中太郎さん": {"note": "tanaka@example.com に共有してください"}},
        )
        assert resumed["status"] in ("awaiting_human", "success")
        _assert_absent(agent, first["thread_id"], "田中太郎", "tanaka@example.com", "田中太郎さん")

    def test_ingest_sanitizes_payload_values_without_changing_column_drop_contract(self):
        agent = _build_agent()
        ctx = InvocationContext(
            thread_id="checkpoint-ingress-ingest",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        result = agent.invoke(
            "",
            ctx=ctx,
            input_context={
                "mode": "ingest",
                "ingest_payload": {
                    "ingest_kind": "trip_records",
                    "rows": [
                        {
                            "employee_name": "田中太郎さん",
                            "email": "tanaka@example.com",
                            "notes": "田中太郎さんへ確認",
                            "destination_area": "Kyoto",
                        }
                    ],
                    "org_unit": "tanaka@example.com-team",
                    "version": "田中太郎さん-v1",
                    "effective_date": "2026-01-01 tanaka@example.com",
                },
            },
        )
        assert result["status"] == "success"
        assert {"employee_name", "email"}.issubset(
            result["output"]["json"]["ingest_summary"]["dropped_columns"]
        )
        _assert_absent(agent, "checkpoint-ingress-ingest", "田中太郎", "tanaka@example.com")

    def test_feedback_sanitizes_complaint_text_and_request_params(self):
        agent = _build_agent()
        ctx = InvocationContext(
            thread_id="checkpoint-ingress-feedback",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )
        result = agent.invoke(
            "田中太郎さんから tanaka@example.com へ連絡してほしいという苦情",
            ctx=ctx,
            input_context={
                "mode": "feedback",
                "request_params": {
                    "plan_id": "safe-plan",
                    "田中様": "tanaka@example.com",
                },
            },
        )
        assert result["status"] == "success"
        _assert_absent(agent, "checkpoint-ingress-feedback", "田中太郎", "tanaka@example.com", "田中様")

    def test_unsupported_value_is_rejected_before_checkpoint_or_error_rendering(self):
        agent = _build_agent()
        ctx = InvocationContext(
            thread_id="checkpoint-ingress-unsupported",
            caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        )

        with pytest.raises(TypeError) as exc_info:
            agent.invoke(
                "4名 Kyoto",
                ctx=ctx,
                input_context={"mode": "plan", "request_params": {"unsafe": {"tanaka@example.com"}}},
            )

        assert "set" in str(exc_info.value)
        assert "tanaka@example.com" not in str(exc_info.value)
        config = {"configurable": {"thread_id": "checkpoint-ingress-unsupported"}}
        assert list(agent._compiled.checkpointer.list(config)) == []
