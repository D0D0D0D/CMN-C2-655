# PB-2 / PB-5: Checkpoint Safety Verification (runtime, not just static AST scan)
# docs/02_design.md C-01: participant care/personal identifiers and
# credentials must never appear in ANY persisted checkpoint (inner or outer),
# across the full invoke -> HITL resume -> success lifecycle.

from collections.abc import Iterator
from typing import Any

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Interrupt

from src.graph.graph import CorporateGroupTripPlannerGraph


def _build_agent():
    agent = CorporateGroupTripPlannerGraph(config={"memory_enabled": True,
                                                     "hitl": {"enabled": True, "max_hitl": 24}})
    agent.compile(checkpointer=InMemorySaver())
    return agent


def _walk_checkpoint_strings(value: Any, path: str) -> Iterator[str]:
    """Yield checkpoint strings without hiding an unsupported object type.

    Pending HITL writes contain ``Interrupt`` objects; inspect their explicit
    ``id`` and ``value`` fields. Any other unknown type fails the safety test
    rather than being coerced with ``str()``/``repr()`` past the PII scan.
    """
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


def _all_checkpoint_dumps(agent, outer_thread_id):
    dumps: list[str] = []
    outer_config = {"configurable": {"thread_id": outer_thread_id}}
    for cp_tuple in agent._compiled.checkpointer.list(outer_config):
        for component in ("checkpoint", "metadata", "pending_writes"):
            dumps.extend(
                _walk_checkpoint_strings(
                    getattr(cp_tuple, component), f"outer.{component}"
                )
            )

    main_node = agent._nodes["main"]
    inner_graph = main_node.get_subgraph()
    snap = agent._compiled.get_state(outer_config)
    inner_thread_id = snap.values.get("subgraph_thread_id")
    if inner_thread_id:
        inner_config = {"configurable": {"thread_id": inner_thread_id}}
        for cp_tuple in inner_graph._compiled.checkpointer.list(inner_config):
            for component in ("checkpoint", "metadata", "pending_writes"):
                dumps.extend(
                    _walk_checkpoint_strings(
                        getattr(cp_tuple, component), f"inner.{component}"
                    )
                )
    return dumps


class TestPBCheckpointSafetyPlanFlow:
    """PB-2/PB-5, full lifecycle: no credential/JWT patterns and no raw
    participant-identifier leakage across suspend -> resume -> success."""

    def test_no_credential_pattern_in_any_checkpoint(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {"mode": "plan", "request_params": {"party_size": 6, "destination_hint": "Kyoto"},
                          "care_notes": "vegetarian"}
        first = agent.invoke("", ctx=ctx, input_context=input_context)
        agent.resume(thread_id=first["thread_id"], feedback="承認します")

        dumps = _all_checkpoint_dumps(agent, first["thread_id"])
        for dump in dumps:
            assert "sk-" not in dump  # OpenAI-shaped key
            assert "eyJ" not in dump  # JWT-shaped token
            assert "AKIA" not in dump  # AWS access key

    def test_participant_identifier_never_leaks_via_resume(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {"mode": "plan", "request_params": {"party_size": 6, "destination_hint": "Kyoto"}}
        first = agent.invoke("", ctx=ctx, input_context=input_context)

        secret_name, secret_email = "山田花子", "yamada@example.com"
        agent.resume(thread_id=first["thread_id"],
                      feedback=f"{secret_name}様のご希望で、連絡先{secret_email}にも共有をお願いします。")

        dumps = _all_checkpoint_dumps(agent, first["thread_id"])
        for dump in dumps:
            assert secret_name not in dump
            assert secret_email not in dump

    def test_care_requirements_are_anonymized_aggregate_only(self):
        """State/checkpoint must carry only {category, count} — never a
        name<->need linkage (C-01)."""
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {"mode": "plan", "request_params": {"party_size": 4, "destination_hint": "Kyoto"},
                          "care_notes": "vegetarian, wheelchair"}
        first = agent.invoke("", ctx=ctx, input_context=input_context)
        snap = agent._compiled.get_state({"configurable": {"thread_id": first["thread_id"]}})
        care = snap.values.get("care_requirements", [])
        for c in care:
            assert set(c.keys()) == {"category", "count"}
