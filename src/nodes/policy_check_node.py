"""AgentCore Platform v1.0 — CMN-C2-655 PolicyCheckNode.

docs/02_design.md §Node Configuration — 内側: `policy_check`.

Checks travel_policy DB for constraints (per-person cap, lodging rules,
approval flow, prohibitions) with citation. Degrades to `policy_status =
"unverified"` when no policy is registered for the org unit — never blocks
the plan flow on missing policy (docs/02_design.md Usability §5).
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.policy_service import PolicyService


class PolicyCheckNode(FunctionNode):
    """Checks travel policy constraints with citation; degrades gracefully."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, policy_service: PolicyService | None = None) -> None:
        super().__init__()
        self._policy_service = policy_service

    def execute(self, state: dict) -> dict:
        params = state.get("request_params", {})

        if self._policy_service is None:
            emit_trace_event("policy_checked", {"status": "unverified", "reason": "not_wired"}, state)
            return {
                "policy_constraints": [],
                "policy_citations": [],
                "policy_status": "unverified",
                "status": AgentStatus.SUCCESS.value,
            }

        result = self._policy_service.check_constraints(
            org_unit=params.get("org_unit", "default"),
            party_size=params.get("party_size") or 0,
            budget_band=params.get("budget_band", "standard"),
        )

        emit_trace_event("policy_checked", {
            "status": result["status"],
            "constraint_count": len(result["constraints"]),
            "citation_count": len(result["citations"]),
        }, state)

        return {
            "policy_constraints": result["constraints"],
            "policy_citations": result["citations"],
            "policy_status": result["status"],
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
