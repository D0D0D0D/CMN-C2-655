"""AgentCore Platform v1.0 — CMN-C2-655 DispatchNode (inner graph entry point).

docs/02_design.md §Node Configuration — 内側: `dispatch`.

Hydrates the outer GraphNode.extract_input() JSON envelope
(`state["user_input"]`, a JSON string) back into the inner graph's own State
fields: `request_params`, `mode`, `care_requirements`. This is the mandatory
counterpart to PreProcessNode's json.dumps() — see docs/02_design.md
§GraphNode契約: "直列化と復元は対になる契約".
"""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.plan_identifier import new_plan_id

_VALID_MODES = ("ingest", "plan", "feedback")


class DispatchNode(FunctionNode):
    """Inner graph entry point — hydrates the JSON envelope into inner State."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        raw = state.get("user_input", "")
        try:
            envelope = json.loads(raw) if raw else {}
        except (json.JSONDecodeError, TypeError) as e:
            emit_trace_event("dispatch_parse_error", {"error": str(e)}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"DispatchNode: failed to parse user_input JSON envelope: {e}"],
            }

        mode = envelope.get("mode")
        if mode not in _VALID_MODES:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"DispatchNode: unknown/missing mode '{mode}' in envelope"],
            }

        result: dict = {
            "mode": mode,
            "request_params": envelope.get("request_params", {}),
            "care_requirements": envelope.get("care_requirements", []),
            "status": AgentStatus.SUCCESS.value,
        }
        if mode == "plan":
            # Generated once here (dispatch runs only on the initial invoke —
            # LangGraph resume re-enters at the suspended hitl_refine
            # checkpoint, not at dispatch) so plan_id stays stable across the
            # whole HITL refine loop and is usable as the /feedback key.
            # Digit-free by construction — see src/services/plan_identifier.py
            # for why a UUID cannot be used on a scanned surface.
            result["plan_id"] = new_plan_id()

        emit_trace_event("dispatch_hydrated", {"mode": mode}, state)
        return result
