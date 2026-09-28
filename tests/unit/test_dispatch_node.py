# CMN-C2-655 — Unit Tests: DispatchNode

import json

from framework.schemas.agent_status import AgentStatus
from src.nodes.dispatch_node import DispatchNode


class TestDispatchNode:
    def setup_method(self):
        self.node = DispatchNode()

    def test_hydrates_plan_mode(self):
        envelope = json.dumps({
            "mode": "plan",
            "request_params": {"party_size": 5},
            "care_requirements": [{"category": "vegetarian", "count": 1}],
        })
        state = {"user_input": envelope, "caller_trust_level": "VERIFIED_EXTERNAL",
                 "status": "pending", "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["mode"] == "plan"
        assert result["request_params"]["party_size"] == 5
        assert "plan_id" in result and result["plan_id"]

    def test_hydrates_ingest_mode_no_plan_id(self):
        envelope = json.dumps({"mode": "ingest", "request_params": {}, "care_requirements": []})
        state = {"user_input": envelope, "caller_trust_level": "VERIFIED_EXTERNAL",
                 "status": "pending", "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "plan_id" not in result

    def test_invalid_json_errors(self):
        state = {"user_input": "not json", "caller_trust_level": "VERIFIED_EXTERNAL",
                 "status": "pending", "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_unknown_mode_errors(self):
        envelope = json.dumps({"mode": "bogus", "request_params": {}})
        state = {"user_input": envelope, "caller_trust_level": "VERIFIED_EXTERNAL",
                 "status": "pending", "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
