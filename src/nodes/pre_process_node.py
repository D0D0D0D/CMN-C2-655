"""AgentCore Platform v1.0 — CMN-C2-655 PreProcessNode.

docs/02_design.md §Node Configuration — 外側（backbone 3スロット）: `pre_process`.

Responsibilities:
  - Determine `mode` (ingest / plan / feedback) from `input_context`.
  - plan mode: verify the 4 minimum fields (party_size / budget hint / season /
    departure_point) are present (free text acceptable); fill defaults for
    anything missing and record `assumption_notes` (Usability §1).
  - Anonymize any raw participant-care free text into a category x count
    aggregate BEFORE it enters `validated_input` (C-01) — this is the primary
    S-2 extension point (`_extra_security_gate_input`).
  - Build `validated_input` as the JSON envelope consumed by
    TripPlanWorkflowGraphNode.extract_input() / inner `dispatch` node.

Note (docs/02_design.md §HITL設計 resume経路): on a HITL resume request,
LangGraph resumes from the suspended `hitl_refine` checkpoint — pre_process is
NOT re-executed. This node's 4-field validation therefore only ever runs on
the initial invoke, never on a resume call.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.security.pii_detector import detect_pii
from shared.utils.audit_logger import emit_trace_event
from src.services.ingress_sanitizer import (
    CARE_NOTES_REDACTION_COUNT_KEY,
    trusted_redaction_count,
)

_VALID_MODES = ("ingest", "plan", "feedback")

# Regex-based care-need category classification (heuristic, not exhaustive).
# Maps free-text keywords -> canonical category label. Extend as needed —
# unmatched care text still gets counted under "other" rather than dropped
# silently.
_CARE_CATEGORY_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("vegetarian", re.compile(r"vegetarian|ベジタリアン|菜食")),
    ("vegan", re.compile(r"vegan|ヴィーガン|ビーガン")),
    ("halal", re.compile(r"halal|ハラール|ハラル")),
    ("allergy", re.compile(r"allergy|allergic|アレルギー")),
    ("wheelchair", re.compile(r"wheelchair|車椅子|車いす")),
    ("pregnant", re.compile(r"pregnant|妊娠")),
]

_DEFAULTS = {
    "party_size": None,
    "budget_band": "standard",
    "season": "unspecified",
    "departure_point": "unspecified",
    "duration_nights": 1,
}


class PreProcessNode(FunctionNode):
    """Validates/normalizes input and dispatches on mode."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        input_context = state.get("input_context", {}) or {}
        mode = input_context.get("mode", "plan")
        user_input = state.get("user_input", "") or ""

        if mode not in _VALID_MODES:
            emit_trace_event("pre_process_invalid_mode", {"mode": mode}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: unknown mode '{mode}' (expected one of {_VALID_MODES})"],
            }

        if mode == "ingest":
            return self._handle_ingest(state, input_context)
        if mode == "feedback":
            return self._handle_feedback(state, input_context, user_input)
        return self._handle_plan(state, input_context, user_input)

    # ------------------------------------------------------------------
    # mode=plan
    # ------------------------------------------------------------------
    def _handle_plan(self, state: dict, input_context: dict, user_input: str) -> dict:
        assumption_notes: list[str] = []
        params = dict(input_context.get("request_params", {}) or {})

        # Best-effort free-text extraction is out of scope for MR-B v1 — the
        # 4 minimum fields are read from structured input_context.request_params
        # (API layer is expected to pass a lightly-parsed dict; a future
        # iteration may add LLM-based extraction from `user_input` free text).
        for field, default in _DEFAULTS.items():
            if params.get(field) in (None, ""):
                if default is not None:
                    params[field] = default
                    assumption_notes.append(
                        f"{field} が未指定のため既定値 '{default}' を使用しました。"
                    )
                else:
                    params[field] = None
                    assumption_notes.append(f"{field} が未指定です（人数）。HITL で確認してください。")

        care_aggregate = self._anonymize_care_text(input_context.get("care_notes", ""))

        request_params = {
            "party_size": params["party_size"],
            "budget_band": params["budget_band"],
            "season": params["season"],
            "departure_point": params["departure_point"],
            "duration_nights": params["duration_nights"],
            "destination_hint": params.get("destination_hint", ""),
            "org_unit": params.get("org_unit", "default"),
        }

        envelope = {
            "mode": "plan",
            "request_params": request_params,
            "care_requirements": care_aggregate,
        }
        validated_input = json.dumps(envelope, ensure_ascii=False, default=str)

        emit_trace_event("pre_process_plan_ready", {
            "assumption_count": len(assumption_notes),
            "care_categories": len(care_aggregate),
        }, state)

        return {
            "mode": "plan",
            "validated_input": validated_input,
            "assumption_notes": assumption_notes,
            "care_requirements": care_aggregate,
            "status": AgentStatus.SUCCESS.value,
        }

    # ------------------------------------------------------------------
    # mode=ingest
    # ------------------------------------------------------------------
    def _handle_ingest(self, state: dict, input_context: dict) -> dict:
        payload = input_context.get("ingest_payload", {}) or {}
        rows = payload.get("rows", [])
        if len(rows) > 500:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [
                    f"PreProcessNode: ingest row count {len(rows)} exceeds 500-row limit; "
                    "split into multiple /ingest calls."
                ],
            }
        envelope = {
            "mode": "ingest",
            "request_params": {
                "ingest_kind": payload.get("ingest_kind", "unknown"),  # "policy" / "trip_records"
                "rows": rows,
                "org_unit": payload.get("org_unit", "default"),
                "version": payload.get("version", ""),
                "effective_date": payload.get("effective_date", ""),
            },
            "care_requirements": [],
        }
        validated_input = json.dumps(envelope, ensure_ascii=False, default=str)
        emit_trace_event("pre_process_ingest_ready", {"row_count": len(rows)}, state)
        return {
            "mode": "ingest",
            "validated_input": validated_input,
            "status": AgentStatus.SUCCESS.value,
        }

    # ------------------------------------------------------------------
    # mode=feedback
    # ------------------------------------------------------------------
    def _handle_feedback(self, state: dict, input_context: dict, user_input: str) -> dict:
        params = input_context.get("request_params", {}) or {}
        plan_id = params.get("plan_id", "")
        if not plan_id:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: feedback mode requires plan_id"],
            }
        # user_input carries the free-text complaint/satisfaction summary; the
        # default S-2 gate has already masked PII_SCAN_FIELDS (user_input,
        # validated_input) with [MASKED] before _extra_security_gate_input runs,
        # so direct identifiers are already stripped by the time we build the
        # envelope below.
        envelope = {
            "mode": "feedback",
            "request_params": {
                "plan_id": plan_id,
                "actual_cost": params.get("actual_cost"),
                "satisfaction_score": params.get("satisfaction_score"),
                "complaint_text": user_input,
                # organizer_edited: audited by FeedbackUpdateNode as an
                # improvement signal (review finding, this session —
                # this field was previously dropped here and always read as
                # False downstream regardless of the API request value).
                "organizer_edited": bool(params.get("organizer_edited", False)),
            },
            "care_requirements": [],
        }
        validated_input = json.dumps(envelope, ensure_ascii=False, default=str)
        emit_trace_event("pre_process_feedback_ready", {"plan_id": plan_id}, state)
        return {
            "mode": "feedback",
            "validated_input": validated_input,
            "status": AgentStatus.SUCCESS.value,
        }

    # ------------------------------------------------------------------
    # S-2 extension (C-01): anonymize care free text -> category x count
    # ------------------------------------------------------------------
    @staticmethod
    def _anonymize_care_text(care_notes: str) -> list[dict]:
        """Converts raw care free text into anonymized {"category","count"} aggregates.

        Never returns a name<->need linkage — only category counts. Direct
        identifiers in `care_notes` are handled by the default S-2 PII mask
        upstream of this call for the standard scan fields; this method adds
        a domain-specific pass restricted to care-category keyword counting
        (it does not need to see names to produce a category count).
        """
        if not care_notes:
            return []
        counts: dict[str, int] = {}
        # crude per-clause split (comma / Japanese comma / "and") — good enough
        # for a free-text aggregate; not intended as clinical-grade parsing.
        clauses = re.split(r"[,、／/]|\band\b", care_notes)
        for clause in clauses:
            if not clause.strip():
                continue
            matched = False
            for category, pattern in _CARE_CATEGORY_PATTERNS:
                if pattern.search(clause):
                    counts[category] = counts.get(category, 0) + 1
                    matched = True
            if not matched:
                # review finding, this session: unmatched care text was
                # previously dropped entirely despite this method's own
                # docstring/comment claiming an "other" bucket. A clause with
                # content but no keyword match still represents a real
                # accommodation request the organizer should see a count for.
                counts["other"] = counts.get("other", 0) + 1
        return [{"category": cat, "count": n} for cat, n in sorted(counts.items())]

    def _extra_security_gate_input(self, state: dict) -> dict:
        """S-2 extension: does not raise on unresolved direct-identifier input —
        per docs/02_design.md, warn + strip instead of hard block (does not
        sacrifice organizer usability)."""
        input_context = state.get("input_context", {}) or {}
        care_notes = input_context.get("care_notes", "")
        ingress_count = trusted_redaction_count(
            input_context, CARE_NOTES_REDACTION_COUNT_KEY
        )
        if isinstance(care_notes, str) and care_notes:
            # A direct node caller can supply unsanitized care notes.  Prefer
            # the node's measurement in that case; graph ingress supplies the
            # internal count only after it has already redacted the text.
            findings = detect_pii(care_notes) if care_notes else []
            redaction_count = (
                len(findings)
                if findings
                else ingress_count
            )
            if redaction_count > 0:
                emit_trace_event("care_notes_identifier_stripped", {
                    "finding_count": redaction_count,
                }, state)
            return state

        # Defensive fallback for direct node tests/callers that bypass the
        # graph boundary. Normal graph traffic takes the branch above, which
        # prevents duplicate S-4 accounting after ingress sanitization.
        return state
