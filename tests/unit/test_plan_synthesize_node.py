# CMN-C2-655 — Unit Tests: PlanSynthesizeNode

from framework.schemas.agent_status import AgentStatus
from src.nodes.plan_synthesize_node import PlanSynthesizeNode
from src.services.llm_client import TripPlannerLLMClient


class _FakeLLM:
    def __init__(self, response_content: str):
        self._content = response_content

    def complete(self, messages):
        return {"content": self._content, "tool_calls": [], "model": "fake"}

    def stream(self, messages):
        yield self._content

    def bind_tools(self, tools):
        return self


class TestPlanSynthesizeNodeFallback:
    def test_no_llm_wired_returns_placeholder_plan(self):
        node = PlanSynthesizeNode(llm_client=None)
        state = {
            "request_params": {"destination_hint": "Hakone", "duration_nights": 2},
            "policy_constraints": [], "policy_citations": [], "ranked_candidates": [],
            "care_requirements": [],
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert len(result["plans"]) == 1
        assert len(result["plans"][0]["itinerary"]) == 2


class TestPlanSynthesizeNodeLLM:
    def test_llm_wired_parses_plans(self):
        import json
        content = json.dumps({"plans": [
            {"destination_area": "Hakone", "itinerary": ["Day 1"], "lodging": "Ryokan",
             "transport": "Bus", "estimated_cost_per_person": 30000, "rationale": "ok",
             "care_accommodations": []},
        ]})
        llm_client = TripPlannerLLMClient(llm=_FakeLLM(content))
        node = PlanSynthesizeNode(llm_client=llm_client, plans_count=1)
        state = {
            "request_params": {"destination_hint": "Hakone", "duration_nights": 1},
            "policy_constraints": [], "policy_citations": [], "ranked_candidates": [],
            "care_requirements": [],
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = node.execute(state)
        assert len(result["plans"]) == 1
        assert result["plans"][0]["destination_area"] == "Hakone"

    def test_refine_feedback_passed_when_adjusting(self):
        node = PlanSynthesizeNode(llm_client=None)
        state = {
            "request_params": {"destination_hint": "Hakone", "duration_nights": 1},
            "policy_constraints": [], "policy_citations": [], "ranked_candidates": [],
            "care_requirements": [], "refine_directive": "adjust",
            "hitl_feedback": "予算を下げてください",
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
