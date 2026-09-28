# CMN-C2-655 — Unit Tests: PreProcessNode

import json

from framework.schemas.agent_status import AgentStatus
from src.nodes.pre_process_node import PreProcessNode


def _base_state(input_context: dict, user_input: str = "") -> dict:
    return {
        "user_input": user_input,
        "input_context": input_context,
        "caller_trust_level": "VERIFIED_EXTERNAL",
        "status": "pending",
        "node_history": [],
        "error_log": [],
    }


class TestPreProcessNodePlanMode:
    def setup_method(self):
        self.node = PreProcessNode()

    def test_plan_mode_minimum_fields_present(self):
        state = _base_state({
            "mode": "plan",
            "request_params": {"party_size": 10, "budget_band": "premium",
                                "season": "spring", "departure_point": "Tokyo",
                                "duration_nights": 2},
        })
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        envelope = json.loads(result["validated_input"])
        assert envelope["request_params"]["party_size"] == 10
        assert result["assumption_notes"] == []

    def test_plan_mode_missing_fields_default_and_note(self):
        state = _base_state({"mode": "plan", "request_params": {}})
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert len(result["assumption_notes"]) > 0
        envelope = json.loads(result["validated_input"])
        assert envelope["request_params"]["budget_band"] == "standard"

    def test_care_notes_anonymized_to_category_count(self):
        state = _base_state({
            "mode": "plan", "request_params": {"party_size": 5},
            "care_notes": "vegetarian, wheelchair",
        })
        result = self.node.execute(state)
        categories = {c["category"] for c in result["care_requirements"]}
        assert "vegetarian" in categories
        assert "wheelchair" in categories
        # C-01: no raw name/identifier leaks into care_requirements
        for c in result["care_requirements"]:
            assert set(c.keys()) == {"category", "count"}

    def test_unknown_mode_errors(self):
        state = _base_state({"mode": "bogus"})
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value


class TestPreProcessNodeIngestMode:
    def setup_method(self):
        self.node = PreProcessNode()

    def test_ingest_row_limit_enforced(self):
        rows = [{"a": 1}] * 501
        state = _base_state({
            "mode": "ingest",
            "ingest_payload": {"ingest_kind": "policy", "rows": rows},
        })
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_ingest_under_limit_succeeds(self):
        rows = [{"a": 1}] * 10
        state = _base_state({
            "mode": "ingest",
            "ingest_payload": {"ingest_kind": "policy", "rows": rows, "org_unit": "default"},
        })
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value


class TestPreProcessNodeFeedbackMode:
    def setup_method(self):
        self.node = PreProcessNode()

    def test_feedback_requires_plan_id(self):
        state = _base_state({"mode": "feedback", "request_params": {}})
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_feedback_with_plan_id_succeeds(self):
        state = _base_state({"mode": "feedback", "request_params": {"plan_id": "p1"}}, user_input="great trip")
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        envelope = json.loads(result["validated_input"])
        assert envelope["request_params"]["plan_id"] == "p1"


class TestPreProcessNodeContract:
    def test_execute_signature(self):
        import inspect
        sig = inspect.signature(PreProcessNode.execute)
        params = list(sig.parameters.keys())
        assert params[1] == "state"
        assert "_invoke_impl" not in PreProcessNode.__dict__
