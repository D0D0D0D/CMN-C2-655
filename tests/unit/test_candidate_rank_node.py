# CMN-C2-655 — Unit Tests: CandidateRankNode

from framework.schemas.agent_status import AgentStatus
from src.nodes.candidate_rank_node import CandidateRankNode


class _FakeTripKB:
    def __init__(self, matches):
        self._matches = matches

    def find_similar_trips(self, **kwargs):
        return self._matches


class TestCandidateRankNode:
    def test_excludes_over_policy_cap(self):
        kb = _FakeTripKB([
            {"metadata": {"destination_area": "A", "satisfaction_score": 5.0,
                           "estimated_cost_per_person": 100000, "complaint_flags": []}},
            {"metadata": {"destination_area": "B", "satisfaction_score": 3.0,
                           "estimated_cost_per_person": 20000, "complaint_flags": []}},
        ])
        node = CandidateRankNode(trip_kb_service=kb)
        state = {
            "request_params": {"destination_hint": "A", "season": "spring", "budget_band": "standard", "party_size": 5},
            "policy_constraints": [{"constraint_type": "per_person_cap", "limit_value": 50000}],
            "care_requirements": [],
            "web_results": [],
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        destinations = [c["destination_area"] for c in result["ranked_candidates"]]
        assert "A" not in destinations  # excluded: over cap
        assert "B" in destinations

    def test_penalizes_complaint_flags(self):
        kb = _FakeTripKB([
            {"metadata": {"destination_area": "A", "satisfaction_score": 4.0, "complaint_flags": ["delay", "cleanliness"]}},
            {"metadata": {"destination_area": "B", "satisfaction_score": 3.5, "complaint_flags": []}},
        ])
        node = CandidateRankNode(trip_kb_service=kb)
        state = {
            "request_params": {"destination_hint": "A", "season": "spring", "budget_band": "standard", "party_size": 5},
            "policy_constraints": [],
            "care_requirements": [],
            "web_results": [],
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = node.execute(state)
        ranked = result["ranked_candidates"]
        # B (no complaints, lower raw satisfaction) should outrank A after penalty
        assert ranked[0]["destination_area"] == "B"
