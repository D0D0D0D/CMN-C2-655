# tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py
#
# PB-7: HITL Interrupt Propagation.
#
# CONDITIONAL: the file is required by exact name for
# every template, but its assertions run only when config/config.yaml declares
# hitl.enabled: true. This template DOES enable HITL, so the assertions below
# execute for real — they are not skipped.
#
# What PB-7 proves here, in two directions:
#
#   (A) hitl_allowed=True  — interrupt() raised inside hitl_refine's execute()
#       produces a GraphInterrupt (a GraphBubbleUp subclass) that propagates
#       through BaseNode.__call__() WITHOUT being swallowed by the generic
#       `except Exception` error boundary. A swallowed interrupt would surface
#       as status=error with a traceback in error_log; the correct outcome is
#       status=AWAITING_HUMAN.
#
#   (B) hitl_allowed=False — the mandatory guard suppresses the interrupt
#       entirely, so an
#       automated composition terminates instead of suspending forever. Covered
#       at BOTH the node boundary and the full composed graph path, because the
#       flag reaches this node only after AgentBaseGraph -> GraphNode ->
#       inner DomainWorkflowGraph propagation.

from __future__ import annotations

import pathlib
import warnings

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import CorporateGroupTripPlannerGraph
from src.nodes.hitl_refine_node import HitlRefineNode

# ---------------------------------------------------------------------------
# Conditional skip — only runs when config/config.yaml has hitl.enabled: true
# ---------------------------------------------------------------------------

_CONFIG_PATH = pathlib.Path(__file__).parents[2] / "config" / "config.yaml"


def _hitl_enabled() -> bool:
    """Return True when config/config.yaml declares hitl.enabled: true.

    An absent or unreadable config.yaml warns instead of skipping silently —
    either case is ambiguous (never shipped / broken
    vs. genuinely non-HITL) and should not look like a clean auto-waiver.
    """
    if not _CONFIG_PATH.exists():
        warnings.warn(
            f"{_CONFIG_PATH} not found — PB-7 skipped without verifying hitl.enabled. "
            "If this template calls interrupt(), ship config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    try:
        import yaml  # pyyaml==6.0.1 — pinned in scaffold/pyproject.toml dependencies

        data = yaml.safe_load(_CONFIG_PATH.read_text())
    except Exception as exc:
        warnings.warn(
            f"{_CONFIG_PATH} could not be read as YAML ({exc}) — PB-7 skipped without "
            "verifying hitl.enabled. If this template calls interrupt(), fix config/config.yaml "
            "before release.",
            stacklevel=2,
        )
        return False
    hitl = (data or {}).get("hitl", {}) if isinstance(data, dict) else None
    if not isinstance(hitl, dict):
        warnings.warn(
            f"{_CONFIG_PATH} does not have the expected 'hitl:' mapping shape — PB-7 skipped "
            "without verifying hitl.enabled. If this template calls interrupt(), fix "
            "config/config.yaml before release.",
            stacklevel=2,
        )
        return False
    return bool(hitl.get("enabled", False))


pytestmark = pytest.mark.skipif(
    not _hitl_enabled(),
    reason="config/config.yaml does not set hitl.enabled: true — PB-7 not applicable",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PLAN_INPUT_CONTEXT = {
    "mode": "plan",
    "request_params": {"party_size": 6, "destination_hint": "Kyoto"},
}


def _agent() -> CorporateGroupTripPlannerGraph:
    agent = CorporateGroupTripPlannerGraph(
        config={"memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 24}}
    )
    agent.compile(checkpointer=InMemorySaver())
    return agent


def _agent_without_checkpointer() -> CorporateGroupTripPlannerGraph:
    agent = CorporateGroupTripPlannerGraph(config={})
    agent.compile()
    return agent


def _node_state(*, hitl_allowed: bool) -> dict:
    """Minimal state that reaches HitlRefineNode.execute()'s interrupt() point."""
    return {
        "plans": [
            {"destination_area": "Hakone"},
            {"destination_area": "Kyoto"},
            {"destination_area": "Beppu"},
        ],
        "caller_trust_level": "VERIFIED_EXTERNAL",
        "status": "pending",
        "node_history": [],
        "error_log": [],
        "quality_notes": ["既存の注記は保持される"],
        "hitl_allowed": hitl_allowed,
    }


# ---------------------------------------------------------------------------
# (A) hitl_allowed=True — interrupt propagates, never swallowed
# ---------------------------------------------------------------------------


def test_pb7_hitl_interrupt_propagates() -> None:
    """GraphInterrupt reaches the LangGraph engine; status is NOT error."""
    agent = _agent()
    ctx = InvocationContext(session_id="pb7-a", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)

    result = agent.invoke("", ctx=ctx, input_context=_PLAN_INPUT_CONTEXT)

    assert result["status"] == AgentStatus.AWAITING_HUMAN.value
    assert result["status"] != AgentStatus.ERROR.value
    assert result["thread_id"]


def test_pb7_interrupt_not_swallowed_as_error() -> None:
    """A swallowed GraphInterrupt would appear as status=error + a traceback in
    error_log. Assert the application error boundary did not fire."""
    agent = _agent()
    ctx = InvocationContext(session_id="pb7-a2", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)

    result = agent.invoke("", ctx=ctx, input_context=_PLAN_INPUT_CONTEXT)

    assert result["status"] != AgentStatus.ERROR.value
    assert not result.get("error_log"), f"error boundary fired on interrupt: {result.get('error_log')}"


def test_pb7_no_checkpointer_raises_at_invoke_not_hang() -> None:
    """Sanity companion: without a checkpointer, interrupt() has nothing to
    suspend into — the framework fails fast rather than hanging."""
    agent = _agent_without_checkpointer()
    ctx = InvocationContext(session_id="pb7-a3", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)

    with pytest.raises(RuntimeError, match="checkpointer"):
        agent.invoke("", ctx=ctx, input_context=_PLAN_INPUT_CONTEXT)


# ---------------------------------------------------------------------------
# (B) hitl_allowed=False — mandatory guard prevents the composition deadlock
#     (the mandatory guard against a composition deadlock)
# ---------------------------------------------------------------------------


def test_pb7_hitl_allowed_false_skips_interrupt_at_node_boundary() -> None:
    """The guard is checked before interrupt(), so calling the node through
    __call__() (S-1 gate included) returns normally instead of suspending."""
    node = HitlRefineNode(llm_client=None)

    result = node(_node_state(hitl_allowed=False))  # via __call__, not execute()

    assert result["status"] == AgentStatus.SUCCESS.value
    # Routing contract preserved: "approve" is the only directive that reaches
    # END in DomainWorkflowGraph.route_after_hitl_refine().
    assert result["refine_directive"] == "approve"
    # The organizer never chose — the selection must stay unmade rather than
    # silently finalizing plans[0] as if it had been approved.
    assert result["selected_plan"] == {}
    assert result["hitl_draft"]
    # Pre-existing quality notes are preserved (State has no list reducer —
    # returning a bare list would drop earlier nodes' notes).
    assert "既存の注記は保持される" in result["quality_notes"]
    assert any("HITL 未実施" in note for note in result["quality_notes"])


def test_pb7_guard_is_what_suppresses_the_interrupt(monkeypatch) -> None:
    """Contrast case proving the guard — and not some unrelated short-circuit —
    is what suppressed the interrupt above.

    Asserted against a spy rather than an error message: keying on the
    "outside of a runnable context" RuntimeError text would couple this proof
    to LangGraph's wording and could go green on an unrelated failure carrying
    the same substring. Real GraphInterrupt propagation is asserted on the
    graph path in test_pb7_hitl_interrupt_propagates().
    """
    import src.nodes.hitl_refine_node as hitl_refine_module

    calls: list = []

    def _spy(payload):
        calls.append(payload)
        return "承認します"

    monkeypatch.setattr(hitl_refine_module, "interrupt", _spy)
    node = HitlRefineNode(llm_client=None)

    node(_node_state(hitl_allowed=False))
    assert calls == [], "hitl_allowed=False must never reach interrupt()"

    node(_node_state(hitl_allowed=True))
    assert len(calls) == 1, "the identical state with hitl_allowed=True must reach interrupt()"


def test_pb7_hitl_allowed_false_composed_path_terminates() -> None:
    """Required proof: invoke the COMPOSED path with
    hitl_allowed=False and verify no GraphInterrupt is raised.

    The flag travels AgentBaseGraph state -> TripPlanWorkflowGraphNode
    (GraphNode, propagate_hitl=True -> `propagate_hitl and hitl_allowed`) ->
    inner DomainWorkflowGraph invocation context -> HitlRefineNode. A guard that
    only worked at the node boundary would still deadlock here.
    """
    agent = _agent()
    ctx = InvocationContext(
        session_id="pb7-b",
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=False,
    )

    result = agent.invoke("", ctx=ctx, input_context=_PLAN_INPUT_CONTEXT)

    # Terminated — never suspended.
    assert result["status"] == AgentStatus.SUCCESS.value
    assert result["status"] != AgentStatus.AWAITING_HUMAN.value
    assert not result.get("error_log"), f"unexpected error: {result.get('error_log')}"

    # The synthesized plans are still delivered; only the human selection is
    # absent, and that absence is stated in the output rather than papered over
    # by finalizing plans[0] (post_process selection_pending branch).
    payload = (result.get("output") or {}).get("json") or {}
    assert payload.get("selection_status") == "pending_organizer"
    assert payload.get("candidate_plans"), "candidate plans must survive the automated path"
    assert payload.get("plan") == {}, "no plan may be presented as chosen"
    assert any("HITL 未実施" in note for note in payload.get("quality_notes", []))

    markdown = (result.get("output") or {}).get("markdown", "")
    assert "幹事による選定が未了" in markdown
    assert "## 行程表" not in markdown, "a single itinerary implies a decision nobody made"
