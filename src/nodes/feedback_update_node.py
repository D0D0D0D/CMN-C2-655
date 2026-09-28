"""AgentCore Platform v1.0 — CMN-C2-655 FeedbackUpdateNode.

docs/02_design.md §Node Configuration — 内側: `feedback_update`.

Receives plan_id + actual cost/satisfaction/complaint free text, strips
direct identifiers from the complaint text (via
src.services.identifier_sanitizer.sanitize_identifiers — see that module's
docstring for the KNOWN GAP found in shared.security.pii_detector.detect_pii
during MR-B integration testing: no Japanese-name detection, and a JP-phone
trailing-\\b boundary bug) + LLM categorization into complaint_flags, and
UPDATEs trip_records + past-trip KB. Never stores employee names in
DB/KB/State (C-01). Best-effort: a DB write failure keeps status=success
with a quality note rather than failing the whole invoke (docs/02_design.md).
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.identifier_sanitizer import sanitize_identifiers
from src.services.llm_client import TripPlannerLLMClient
from src.services.trip_kb_service import TripKBService
from src.services.trip_records_service import TripRecordsService

_COMPLAINT_KEYWORD_FLAGS = {
    "late": "delay", "遅延": "delay", "遅れ": "delay",
    "dirty": "cleanliness", "汚": "cleanliness", "不潔": "cleanliness",
    "expensive": "cost_overrun", "高すぎ": "cost_overrun", "予算オーバー": "cost_overrun",
    "food": "food_quality", "食事": "food_quality", "料理": "food_quality",
    "cancel": "cancellation", "キャンセル": "cancellation",
}


class FeedbackUpdateNode(FunctionNode):
    """Sanitizes complaint free text, categorizes flags, updates DB/KB."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, trip_records_service: TripRecordsService | None = None,
                 trip_kb_service: TripKBService | None = None,
                 llm_client: TripPlannerLLMClient | None = None) -> None:
        super().__init__()
        self._trip_records_service = trip_records_service
        self._trip_kb_service = trip_kb_service
        self._llm_client = llm_client

    def execute(self, state: dict) -> dict:
        params = state.get("request_params", {})
        plan_id = params.get("plan_id", "")
        complaint_text = params.get("complaint_text", "") or ""

        sanitized_text, redaction_count = sanitize_identifiers(complaint_text)
        if redaction_count:
            emit_trace_event("hitl_response_sanitized", {
                "redaction_count": redaction_count, "context": "feedback_update",
            }, state)

        complaint_flags = self._categorize(sanitized_text)
        organizer_edited = bool(params.get("organizer_edited", False))

        if self._trip_records_service is None:
            emit_trace_event("feedback_recorded", {"plan_id": plan_id, "updated": False,
                                                     "reason": "not_wired"}, state)
            return {
                "feedback_result": {"plan_id": plan_id, "updated": False, "reason": "service not wired"},
                "status": AgentStatus.SUCCESS.value,
            }

        try:
            db_result = self._trip_records_service.update_feedback(
                plan_id=plan_id,
                actual_cost=params.get("actual_cost"),
                satisfaction_score=params.get("satisfaction_score"),
                complaint_flags=complaint_flags,
                organizer_edited=organizer_edited,
            )
            if self._trip_kb_service is not None:
                self._trip_kb_service.upsert_trip_record(
                    plan_id=plan_id,
                    destination_area=params.get("destination_area", ""),
                    season=params.get("season", ""),
                    party_size=int(params.get("party_size") or 0),
                    budget_band=params.get("budget_band", ""),
                    summary_text=sanitized_text,
                    satisfaction_score=params.get("satisfaction_score"),
                    complaint_flags=complaint_flags,
                )
        except Exception as e:  # noqa: BLE001 — best-effort per design; never fail the whole invoke
            emit_trace_event("feedback_recorded", {"plan_id": plan_id, "updated": False,
                                                     "error": str(e)}, state)
            return {
                "feedback_result": {"plan_id": plan_id, "updated": False, "reason": str(e)},
                "quality_notes": list(state.get("quality_notes", [])) +
                    ["フィードバックの保存に失敗しました（担当者に個別連絡してください）。"],
                "status": AgentStatus.SUCCESS.value,
            }

        emit_trace_event("feedback_recorded", {
            "plan_id": plan_id, "updated": db_result.get("updated", False),
            "complaint_flag_count": len(complaint_flags),
        }, state)

        return {
            "feedback_result": {"plan_id": plan_id, "updated": db_result.get("updated", False),
                                 "complaint_flags": complaint_flags, "organizer_edited": organizer_edited},
            "status": AgentStatus.SUCCESS.value,
        }

    def _categorize(self, sanitized_text: str) -> list[str]:
        """LLM categorization when wired (nuanced flags); keyword heuristic
        otherwise or on LLM failure (review finding, this session: the
        LLM path was previously dead code — a bare `pass` — so even a wired
        LLM client never actually classified anything)."""
        if self._llm_client is not None:
            try:
                flags = self._llm_client.categorize_complaint(sanitized_text)
                if flags:
                    return flags
            except Exception:  # noqa: BLE001 — degrade to keyword heuristic, never crash the node
                pass
        return self._keyword_categorize(sanitized_text)

    @staticmethod
    def _keyword_categorize(sanitized_text: str) -> list[str]:
        lowered = sanitized_text.lower()
        flags = set()
        for keyword, flag in _COMPLAINT_KEYWORD_FLAGS.items():
            if keyword in sanitized_text or keyword in lowered:
                flags.add(flag)
        return sorted(flags)

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
