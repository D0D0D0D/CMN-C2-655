# CMN-C2-655 — Unit Tests: PolicyCheckNode

from framework.schemas.agent_status import AgentStatus
from src.nodes.policy_check_node import PolicyCheckNode
from src.services.policy_service import PolicyService


class _FakeDB:
    def __init__(self, row=None):
        self._row = row

    def fetch_one(self, query, params=None):
        return self._row

    def fetch_many(self, query, params=None):
        return []

    def execute(self, query, params=None):
        return None


class TestPolicyCheckNodeUnverified:
    def test_no_policy_service_degrades_unverified(self):
        node = PolicyCheckNode(policy_service=None)
        state = {"request_params": {"org_unit": "default", "party_size": 5},
                 "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
                 "node_history": [], "error_log": []}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["policy_status"] == "unverified"

    def test_missing_policy_row_degrades_unverified(self):
        service = PolicyService(_FakeDB(row=None))
        node = PolicyCheckNode(policy_service=service)
        state = {"request_params": {"org_unit": "default", "party_size": 5},
                 "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
                 "node_history": [], "error_log": []}
        result = node.execute(state)
        assert result["policy_status"] == "unverified"


class TestPolicyCheckNodeVerified:
    def test_verified_with_citations(self):
        row = {"version": "v1", "clauses": [
            {"clause_no": "3", "text": "一人当たり上限5万円", "constraint_type": "per_person_cap",
             "limit_value": 50000},
        ]}
        service = PolicyService(_FakeDB(row=row))
        node = PolicyCheckNode(policy_service=service)
        state = {"request_params": {"org_unit": "default", "party_size": 5},
                 "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
                 "node_history": [], "error_log": []}
        result = node.execute(state)
        assert result["policy_status"] == "verified"
        assert len(result["policy_citations"]) == 1
        assert result["policy_constraints"][0]["limit_value"] == 50000
