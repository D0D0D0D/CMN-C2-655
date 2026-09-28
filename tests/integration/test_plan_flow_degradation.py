# CMN-C2-655 — Integration Tests: plan-mode graceful degradation
# docs/02_design.md Usability §5: web search failure / policy unverified /
# empty KB must never silently degrade — quality_notes/policy_status must
# surface the degradation to the organizer.

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import CorporateGroupTripPlannerGraph


def _build_agent():
    agent = CorporateGroupTripPlannerGraph(config={"memory_enabled": True,
                                                     "hitl": {"enabled": True, "max_hitl": 24}})
    agent.compile(checkpointer=InMemorySaver())
    return agent


class TestPlanFlowDegradation:
    def test_web_search_unavailable_falls_back_with_quality_note(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {"mode": "plan", "request_params": {"party_size": 8, "destination_hint": "Kyoto"}}
        result = agent.invoke("", ctx=ctx, input_context=input_context)
        assert result["status"] == "awaiting_human"
        # quality_notes propagate through merge_output at suspend time only
        # once resumed to success; verify via resume.
        resumed = agent.resume(thread_id=result["thread_id"], feedback="承認します")
        assert resumed["status"] == "success"
        quality_notes = resumed["output"]["json"]["quality_notes"]
        assert any("過去の旅行実績データ" in n for n in quality_notes)

    def test_policy_unverified_when_no_policy_registered(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {"mode": "plan", "request_params": {"party_size": 8, "destination_hint": "Kyoto"}}
        result = agent.invoke("", ctx=ctx, input_context=input_context)
        resumed = agent.resume(thread_id=result["thread_id"], feedback="承認します")
        assert resumed["output"]["json"]["policy_status"] == "unverified"
        assert "要確認" in resumed["output"]["markdown"] or "規程が未整備" in resumed["output"]["markdown"]

    def test_minimum_fields_missing_uses_defaults_with_assumption_notes(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {"mode": "plan", "request_params": {}}
        result = agent.invoke("", ctx=ctx, input_context=input_context)
        resumed = agent.resume(thread_id=result["thread_id"], feedback="承認します")
        assert len(resumed["output"]["json"]["assumption_notes"]) > 0
