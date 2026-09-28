# CMN-C2-655 — Integration Tests: /feedback mode

from uuid import UUID

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import CorporateGroupTripPlannerGraph


def _build_agent():
    agent = CorporateGroupTripPlannerGraph(config={"memory_enabled": True,
                                                     "hitl": {"enabled": True, "max_hitl": 24}})
    agent.compile(checkpointer=InMemorySaver())
    return agent


class TestFeedbackFlow:
    def test_feedback_without_plan_id_errors(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {"mode": "feedback", "request_params": {}}
        result = agent.invoke("", ctx=ctx, input_context=input_context)
        assert result["status"] == "error"

    def test_feedback_with_plan_id_succeeds(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {"mode": "feedback",
                          "request_params": {"plan_id": "p-1", "actual_cost": 30000,
                                              "satisfaction_score": 4.0}}
        result = agent.invoke("料理が美味しかったです", ctx=ctx, input_context=input_context)
        assert result["status"] == "success"

    def test_feedback_complaint_identifier_not_leaked_into_output(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {"mode": "feedback", "request_params": {"plan_id": "p-1"}}
        result = agent.invoke("田中太郎さんから、料理が遅くて残念という声がありました。連絡先はtanaka@example.comです。",
                               ctx=ctx, input_context=input_context)
        assert result["status"] == "success"
        import json
        dump = json.dumps(result, default=str, ensure_ascii=False)
        assert "田中太郎" not in dump
        assert "tanaka@example.com" not in dump


class TestPlanIdRoundTrip:
    """The identifier a plan run hands out must come back through the
    feedback route byte for byte. Both directions cross a text scanner, so a
    digit-shaped identifier is either blocked on the way out or rewritten on
    the way in — see src/services/plan_identifier.py."""

    # A 128-bit value the shared PII detector reads as a national ID number
    # when it is written as a UUID.
    _MISREAD_UUID = "72648243-96ba-415b-b01b-695503485898"

    def test_issued_plan_id_is_echoed_unchanged_by_the_feedback_route(self, monkeypatch):
        import src.services.plan_identifier as plan_identifier

        monkeypatch.setattr(
            plan_identifier, "uuid4", lambda: UUID(self._MISREAD_UUID)
        )
        agent = _build_agent()

        first = agent.invoke(
            "",
            ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
            input_context={
                "mode": "plan",
                "request_params": {
                    "destination": "箱根",
                    "participants": 10,
                    "budget_per_person": 30000,
                },
            },
        )
        approved = agent.resume(thread_id=first["thread_id"], feedback="承認します")
        assert approved["status"] == "success"
        issued_plan_id = approved["output"]["json"]["plan_id"]

        recorded = agent.invoke(
            "料理が美味しかったです",
            ctx=InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
            input_context={
                "mode": "feedback",
                "request_params": {"plan_id": issued_plan_id, "actual_cost": 30000},
            },
        )
        assert recorded["status"] == "success"
        assert recorded["output"]["json"]["plan_id"] == issued_plan_id
