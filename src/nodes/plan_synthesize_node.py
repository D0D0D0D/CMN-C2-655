"""AgentCore Platform v1.0 — CMN-C2-655 PlanSynthesizeNode.

docs/02_design.md §Node Configuration — 内側: `plan_synthesize`.

LLM-synthesizes N policy-compliant itinerary plans from ranked candidates.
Re-synthesizes with adjusted weighting when `hitl_feedback`/`refine_directive`
indicate the organizer requested changes (adjust) — see hitl_refine_node.py
for the resume-side interpretation that sets `refine_directive`.
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.llm_client import TripPlannerLLMClient


class PlanSynthesizeNode(FunctionNode):
    """Synthesizes N plan candidates; re-synthesizes on HITL adjust feedback."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm_client: TripPlannerLLMClient | None = None, *, plans_count: int = 3) -> None:
        super().__init__()
        self._llm_client = llm_client
        self._plans_count = plans_count

    def execute(self, state: dict) -> dict:
        params = state.get("request_params", {})
        refine_feedback = None
        if state.get("refine_directive") == "adjust":
            hitl_feedback = state.get("hitl_feedback")
            refine_feedback = hitl_feedback if isinstance(hitl_feedback, str) else None

        if self._llm_client is not None:
            plans = self._llm_client.synthesize_plans(
                request_params=params,
                policy_constraints=state.get("policy_constraints", []),
                policy_citations=state.get("policy_citations", []),
                ranked_candidates=state.get("ranked_candidates", []),
                care_requirements=state.get("care_requirements", []),
                plans_count=self._plans_count,
                refine_feedback=refine_feedback,
            )
        else:
            plans = self._fallback_plans(params)

        emit_trace_event("plans_synthesized", {"plan_count": len(plans),
                                                "is_refine": refine_feedback is not None}, state)

        return {"plans": plans, "status": AgentStatus.SUCCESS.value}

    @staticmethod
    def _fallback_plans(params: dict) -> list[dict]:
        """Deterministic placeholder plans when no LLM client is wired (dev/test)."""
        nights = max(1, int(params.get("duration_nights") or 1))
        return [{
            "destination_area": params.get("destination_hint") or "（未確定）",
            "itinerary": [f"Day {i + 1}: 未確定" for i in range(nights)],
            "lodging": "（未確定）",
            "transport": "（未確定）",
            "estimated_cost_per_person": None,
            "rationale": "LLM client not wired — placeholder plan.",
            "care_accommodations": [],
        }]

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
