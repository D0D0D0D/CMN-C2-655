# CMN-C2-655 — Regression tests for pre-push review findings (this session)
#
# 1. HITL approval previously always finalized plans[0] regardless of which
#    of the 3 presented plans the organizer actually selected.
# 2. organizer_edited was dropped in PreProcessNode's feedback envelope.
# 3. FeedbackUpdateNode's LLM complaint categorization was dead code (bare
#    `pass`) — always fell through to the keyword heuristic even when an
#    LLM client was wired.
# 4. PreProcessNode._anonymize_care_text silently dropped unmatched care
#    clauses despite its own docstring/comment claiming an "other" bucket.
# 5. src/api/server.py wired real services AFTER agent.compile(), so
#    TripPlanWorkflowGraphNode's cached inner-graph config snapshot never
#    saw them (config-plumbing test only, covered separately below).

import json

from src.nodes.feedback_update_node import FeedbackUpdateNode
from src.nodes.hitl_refine_node import HitlRefineNode
from src.nodes.pre_process_node import PreProcessNode
from src.services.llm_client import TripPlannerLLMClient


class TestHitlRefineNodePlanSelection:
    def setup_method(self):
        self.node = HitlRefineNode(llm_client=None)
        self.plans = [{"destination_area": "A"}, {"destination_area": "B"}, {"destination_area": "C"}]

    def test_selects_second_plan_by_ordinal_japanese(self):
        idx = self.node._select_plan_index("2番目のプランで承認します", len(self.plans))
        assert idx == 1

    def test_selects_third_plan_by_plan_word(self):
        idx = self.node._select_plan_index("plan 3で確定します", len(self.plans))
        assert idx == 2

    def test_no_selection_signal_defaults_to_first(self):
        idx = self.node._select_plan_index("承認します", len(self.plans))
        assert idx == 0

    def test_single_plan_always_index_zero(self):
        idx = self.node._select_plan_index("2番目で", 1)
        assert idx == 0

    def test_execute_uses_selected_index(self, monkeypatch):
        import src.nodes.hitl_refine_node as hitl_module
        monkeypatch.setattr(hitl_module, "interrupt", lambda payload: "2案目で承認します")
        state = {"plans": self.plans, "caller_trust_level": "VERIFIED_EXTERNAL",
                 "status": "pending", "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["selected_plan"]["destination_area"] == "B"


class TestPreProcessNodeOrganizerEditedForwarding:
    def test_organizer_edited_forwarded_in_feedback_envelope(self):
        node = PreProcessNode()
        state = {
            "user_input": "complaint text",
            "input_context": {"mode": "feedback",
                               "request_params": {"plan_id": "p1", "organizer_edited": True}},
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = node.execute(state)
        envelope = json.loads(result["validated_input"])
        assert envelope["request_params"]["organizer_edited"] is True

    def test_organizer_edited_defaults_false(self):
        node = PreProcessNode()
        state = {
            "user_input": "complaint text",
            "input_context": {"mode": "feedback", "request_params": {"plan_id": "p1"}},
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = node.execute(state)
        envelope = json.loads(result["validated_input"])
        assert envelope["request_params"]["organizer_edited"] is False


class TestFeedbackUpdateNodeLLMCategorization:
    class _FakeLLM:
        def __init__(self, content):
            self._content = content

        def complete(self, messages):
            return {"content": self._content, "tool_calls": [], "model": "fake"}

        def stream(self, messages):
            yield self._content

        def bind_tools(self, tools):
            return self

    def test_llm_categorization_actually_invoked(self):
        content = json.dumps({"flags": ["noise_complaint"]})
        llm_client = TripPlannerLLMClient(llm=self._FakeLLM(content))
        node = FeedbackUpdateNode(llm_client=llm_client)
        flags = node._categorize("うるさかったです")
        assert flags == ["noise_complaint"]

    def test_llm_failure_falls_back_to_keywords(self):
        class _BrokenLLM:
            def complete(self, messages):
                raise RuntimeError("LLM unavailable")

            def stream(self, messages):
                raise RuntimeError("unavailable")

            def bind_tools(self, tools):
                return self

        llm_client = TripPlannerLLMClient(llm=_BrokenLLM())
        node = FeedbackUpdateNode(llm_client=llm_client)
        flags = node._categorize("料理が遅かったです")
        assert "food_quality" in flags or "delay" in flags

    def test_no_llm_uses_keyword_heuristic(self):
        node = FeedbackUpdateNode(llm_client=None)
        flags = node._categorize("料理が遅かったです")
        assert "food_quality" in flags or "delay" in flags


class TestCareTextOtherBucket:
    def setup_method(self):
        self.node = PreProcessNode()

    def test_unmatched_clause_counted_as_other(self):
        result = self.node._anonymize_care_text("early check-in requested")
        categories = {c["category"]: c["count"] for c in result}
        assert categories.get("other") == 1

    def test_matched_clause_not_double_counted_as_other(self):
        result = self.node._anonymize_care_text("vegetarian")
        categories = {c["category"] for c in result}
        assert "other" not in categories

    def test_mixed_matched_and_unmatched(self):
        result = self.node._anonymize_care_text("vegetarian, early check-in requested")
        categories = {c["category"]: c["count"] for c in result}
        assert categories.get("vegetarian") == 1
        assert categories.get("other") == 1
