# CMN-C2-655 — Integration Tests: HITL wall-bounce flow
# PB-7 (interrupt propagation) + resume->success (subgraph cache correctness,
# TourPlanningProcurementRoutingAgent precedent) + resume-response sanitization PB (C-01, MR-B finding).

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import CorporateGroupTripPlannerGraph
from src.services.ingress_sanitizer import INGRESS_REDACTION_COUNT_KEY


def _build_agent():
    agent = CorporateGroupTripPlannerGraph(config={"memory_enabled": True,
                                                     "hitl": {"enabled": True, "max_hitl": 24}})
    agent.compile(checkpointer=InMemorySaver())
    return agent


def _plan_input_context(**overrides):
    ctx = {"mode": "plan", "request_params": {"party_size": 10, "destination_hint": "Hakone"},
           "care_notes": "vegetarian"}
    ctx.update(overrides)
    return ctx


class TestHitlInterruptPropagation:
    """PB-7: interrupt() surfaces as AWAITING_HUMAN at the OUTER boundary,
    not as status=error, and is not swallowed anywhere in the call chain."""

    def test_first_invoke_suspends_awaiting_human(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        result = agent.invoke("", ctx=ctx, input_context=_plan_input_context())
        assert result["status"] == "awaiting_human"
        assert result["thread_id"]
        assert result["error_log"] == []


class TestHitlResumeToSuccess:
    """resume()->success (not just suspend) — catches the subgraph-cache bug
    (TourPlanningProcurementRoutingAgent): a fresh inner-graph instance per get_subgraph() call loses
    the suspended checkpoint and causes an infinite suspend-reinvoke loop
    instead of ever reaching success."""

    def test_resume_approve_reaches_success(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        first = agent.invoke("", ctx=ctx, input_context=_plan_input_context())
        assert first["status"] == "awaiting_human"

        resumed = agent.resume(thread_id=first["thread_id"], feedback="承認します")
        assert resumed["status"] == "success"
        assert resumed["output"] is not None
        assert "markdown" in resumed["output"]
        assert "json" in resumed["output"]

    def test_resume_adjust_then_approve(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        first = agent.invoke("", ctx=ctx, input_context=_plan_input_context())
        thread_id = first["thread_id"]

        adjust_result = agent.resume(thread_id=thread_id, feedback="予算をもう少し下げてください")
        assert adjust_result["status"] == "awaiting_human"  # re-suspended for another round

        final_result = agent.resume(thread_id=thread_id, feedback="承認します")
        assert final_result["status"] == "success"


class TestHitlResponseSanitization:
    """C-01 / user decision 2026-07-06: resume response identifiers must be
    stripped BEFORE they touch State/checkpoint — verified across ALL
    checkpoints (inner + outer), not just the final output."""

    def test_identifier_absent_from_all_checkpoints_after_resume(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        first = agent.invoke("", ctx=ctx, input_context=_plan_input_context())
        thread_id = first["thread_id"]

        secret_name = "田中太郎"
        secret_email = "tanaka@example.com"
        secret_phone = "080-1234-5678"
        raw_feedback = f"{secret_name}さんはベジタリアンなので和食にしてください。連絡先は{secret_email}、電話{secret_phone}です。"

        resumed = agent.resume(thread_id=thread_id, feedback=raw_feedback)
        # regardless of directive (adjust/approve), no leak must occur
        assert resumed["status"] in ("success", "awaiting_human")

        leaked = self._scan_all_checkpoints_for_leak(agent, thread_id,
                                                       [secret_name, secret_email, secret_phone])
        assert leaked == [], f"Identifier leaked into checkpoint(s): {leaked}"

    def test_sanitization_event_emitted_with_count_only(self, monkeypatch):
        import src.nodes.hitl_refine_node as hitl_module

        events = []
        original_emit = hitl_module.emit_trace_event

        def spy_emit(event_type, payload, state):
            events.append((event_type, payload))
            return original_emit(event_type, payload, state)

        monkeypatch.setattr(hitl_module, "emit_trace_event", spy_emit)

        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        first = agent.invoke("", ctx=ctx, input_context=_plan_input_context())
        agent.resume(thread_id=first["thread_id"],
                      feedback="田中太郎さんによろしくお伝えください")

        sanitized_events = [p for t, p in events if t == "hitl_response_sanitized"]
        assert len(sanitized_events) == 1
        assert sanitized_events[0]["redaction_count"] >= 1
        assert set(sanitized_events[0].keys()) == {"redaction_count"}

    def test_resume_drops_spoofed_redaction_count(self, monkeypatch):
        """ATK3: a clean response cannot inject a false HITL S-4 event."""
        import src.nodes.hitl_refine_node as hitl_module

        events = []
        original_emit = hitl_module.emit_trace_event

        def spy_emit(event_type, payload, state):
            events.append((event_type, payload))
            return original_emit(event_type, payload, state)

        monkeypatch.setattr(hitl_module, "emit_trace_event", spy_emit)

        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        first = agent.invoke("", ctx=ctx, input_context=_plan_input_context())

        resumed = agent.resume(
            thread_id=first["thread_id"],
            feedback={
                "text": "予算をもう少し下げてください",
                INGRESS_REDACTION_COUNT_KEY: 999,
            },
        )

        assert resumed["status"] == "awaiting_human"
        sanitized_events = [
            payload
            for event_type, payload in events
            if event_type == "hitl_response_sanitized"
        ]
        assert sanitized_events == []

    @staticmethod
    def _scan_all_checkpoints_for_leak(agent, outer_thread_id, secrets):
        leaked = []
        outer_config = {"configurable": {"thread_id": outer_thread_id}}
        for cp_tuple in agent._compiled.checkpointer.list(outer_config):
            dump = json.dumps(cp_tuple.checkpoint, default=str, ensure_ascii=False)
            for s in secrets:
                if s in dump:
                    leaked.append(f"outer:{s}")

        main_node = agent._nodes["main"]
        inner_graph = main_node.get_subgraph()
        snap = agent._compiled.get_state(outer_config)
        inner_thread_id = snap.values.get("subgraph_thread_id")
        if inner_thread_id:
            inner_config = {"configurable": {"thread_id": inner_thread_id}}
            for cp_tuple in inner_graph._compiled.checkpointer.list(inner_config):
                dump = json.dumps(cp_tuple.checkpoint, default=str, ensure_ascii=False)
                for s in secrets:
                    if s in dump:
                        leaked.append(f"inner:{s}")
        return leaked


class TestHitlNewSearchExplicitOnly:
    """Cat 2 boundary: new_search only fires on an EXPLICIT organizer request
    for a different destination — never an autonomous re-search decision."""

    def test_explicit_new_search_request_routes_back_to_web_search(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        first = agent.invoke("", ctx=ctx, input_context=_plan_input_context())
        thread_id = first["thread_id"]

        result = agent.resume(thread_id=thread_id, feedback="別の目的地を探してください")
        # Explicit new_search re-suspends again at hitl_refine after a fresh
        # web_search -> candidate_rank -> plan_synthesize round-trip.
        assert result["status"] == "awaiting_human"
