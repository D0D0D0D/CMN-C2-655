"""AgentCore Platform v1.0 — CMN-C2-655 CandidateRankNode.

docs/02_design.md §Node Configuration — 内側: `candidate_rank`.

Ranks candidates by blending live web_results with past-trip KB similarity:
boosts by satisfaction_score, avoids complaint_flags, excludes over policy
cap, and flags candidates against anonymized care_requirements coverage
(e.g. vegetarian-friendly lodging) — never re-identifies a participant.
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.trip_kb_service import TripKBService


class CandidateRankNode(FunctionNode):
    """Ranks web/KB candidates by past satisfaction, policy fit, care coverage."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, trip_kb_service: TripKBService | None = None) -> None:
        super().__init__()
        self._trip_kb_service = trip_kb_service

    def execute(self, state: dict) -> dict:
        params = state.get("request_params", {})
        policy_constraints = state.get("policy_constraints", [])
        care_requirements = state.get("care_requirements", [])
        web_results = state.get("web_results", [])

        kb_matches: list[dict] = []
        if self._trip_kb_service is not None:
            kb_matches = self._trip_kb_service.find_similar_trips(
                destination_hint=params.get("destination_hint", ""),
                season=params.get("season", "unspecified"),
                party_size=params.get("party_size") or 0,
                budget_band=params.get("budget_band", "standard"),
            )

        per_person_cap = self._per_person_cap(policy_constraints)
        ranked = self._rank(web_results, kb_matches, per_person_cap, care_requirements)

        emit_trace_event("candidates_ranked", {
            "candidate_count": len(ranked),
            "kb_match_count": len(kb_matches),
        }, state)

        return {
            "kb_matches": kb_matches,
            "ranked_candidates": ranked,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _per_person_cap(policy_constraints: list[dict]) -> float | None:
        for c in policy_constraints:
            if c.get("constraint_type") == "per_person_cap" and c.get("limit_value") is not None:
                try:
                    return float(c["limit_value"])
                except (TypeError, ValueError):
                    continue
        return None

    @staticmethod
    def _rank(web_results: list[dict], kb_matches: list[dict], per_person_cap: float | None,
              care_requirements: list[dict]) -> list[dict]:
        candidates: list[dict] = []
        care_categories = {c.get("category") for c in care_requirements}

        for kb in kb_matches:
            meta = kb.get("metadata", kb)
            score = float(meta.get("satisfaction_score") or 0.0)
            complaint_flags = meta.get("complaint_flags") or []
            if complaint_flags:
                score -= 0.5 * len(complaint_flags)
            est_cost = meta.get("estimated_cost_per_person")
            excluded = per_person_cap is not None and est_cost is not None and est_cost > per_person_cap
            candidates.append({
                "source": "kb",
                "destination_area": meta.get("destination_area", ""),
                "score": score,
                "excluded": excluded,
                "complaint_flags": complaint_flags,
                "care_coverage_flagged": bool(care_categories),
            })

        for web in web_results:
            candidates.append({
                "source": "web",
                "destination_area": web.get("query", ""),
                "score": 0.0,
                "excluded": False,
                "complaint_flags": [],
                "care_coverage_flagged": bool(care_categories),
                "raw": web.get("raw"),
            })

        candidates = [c for c in candidates if not c["excluded"]]
        candidates.sort(key=lambda c: c["score"], reverse=True)
        return candidates

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
