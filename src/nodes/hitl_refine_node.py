"""AgentCore Platform v1.0 — CMN-C2-655 HitlRefineNode.

docs/02_design.md §HITL設計（壁打ち対話＝正常系）. D6 `interrupt()` pattern —
the organizer's wall-bounce dialogue over the 3 synthesized plans is the
CORE value of this template, not an exception path. `max_hitl` is a runaway
safety valve only (config/config.yaml hitl.max_hitl: 24) — this node never
implements its own turn-count breaker (framework/backbone concern only).

Resume-response sanitization (docs/02_design.md §HITL設計, user decision
2026-07-06): the resume path does NOT go through PreProcessNode's S-2
extension (LangGraph resumes directly at this node's suspended checkpoint —
pre_process is not re-executed). So THIS node must independently strip direct
identifiers (name/contact) from the raw organizer response at the very start
of resume-response interpretation, and use ONLY the sanitized text downstream
(refine_directive interpretation, hitl_feedback storage, plan_synthesize
input). Emits `hitl_response_sanitized` with the REMOVAL COUNT ONLY (never
the removed content) when any redaction occurred.

Sanitization is delegated to src.services.identifier_sanitizer.sanitize_identifiers()
(shared with feedback_update_node.py, which has the identical resume-adjacent
gap — see that module's docstring for the KNOWN GAP found in
shared.security.pii_detector.detect_pii during MR-B integration testing).
"""

from __future__ import annotations

import re
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from langgraph.types import interrupt
from shared.utils.audit_logger import emit_trace_event
from src.services.identifier_sanitizer import sanitize_identifiers
from src.services.ingress_sanitizer import (
    INGRESS_REDACTION_COUNT_KEY,
    trusted_redaction_count,
)
from src.services.llm_client import TripPlannerLLMClient

_NEW_SEARCH_KEYWORDS = ("別の目的地", "別の場所", "違う場所", "別の軸", "new destination", "different destination")

# Quality note emitted when the wall-bounce dialogue could not run because the
# invocation forbids HITL (hitl_allowed=False). Surfaced to the organizer via
# post_process's 「品質注記」 section so the unmade selection is never mistaken
# for an approved plan.
_HITL_SKIPPED_NOTE = (
    "HITL 未実施（hitl_allowed=false の自動実行文脈）: "
    "提示した全プランは幹事による選定・条件調整が未了です。"
)

# Plan-selection patterns (review finding, this session: approval
# previously always finalized plans[0] regardless of which of the 3 presented
# plans the organizer actually chose). High precision by design — an
# ambiguous response falls back to plan index 0 rather than guessing wrong.
_PLAN_INDEX_PATTERNS: list["re.Pattern[str]"] = [
    re.compile(r"(\d)\s*(?:番目|案|つ目)"),           # "2番目", "2案", "2つ目"
    re.compile(r"(?:プラン|案|plan)\s*(\d)", re.IGNORECASE),  # "プラン2", "plan 2"
    re.compile(r"^\s*(\d)\s*$"),                        # bare "2"
]
_ORDINAL_WORDS = {"一": 0, "二": 1, "三": 2, "1つ目": 0, "2つ目": 1, "3つ目": 2,
                   "first": 0, "second": 1, "third": 2}


class HitlRefineNode(FunctionNode):
    """D6 interrupt() wall-bounce review node — organizer refine loop."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm_client: TripPlannerLLMClient | None = None) -> None:
        super().__init__()
        self._llm_client = llm_client

    def execute(self, state: dict) -> dict:
        plans = state.get("plans", [])

        # hitl_draft idempotency guard: skip recomputation on resume — the
        # draft presented to the organizer must not silently change between
        # suspend and resume (framework schema 1.4 contract).
        draft = state.get("hitl_draft") or plans

        # MANDATORY hitl_allowed guard — an unconditional interrupt() in a
        # composed pipeline is a production deadlock.
        # This node runs inside DomainWorkflowGraph behind
        # TripPlanWorkflowGraphNode(GraphNode, propagate_hitl=True), and the
        # 1.0.0 wheel's GraphNode.execute() computes
        #     subgraph_hitl_allowed = propagate_hitl and state.get("hitl_allowed", True)
        # then passes it into the inner invocation context. So a parent
        # composition — or a Gateway sending X-HITL-Allowed: false — delivers
        # hitl_allowed=False into THIS state. An unconditional interrupt() would
        # suspend the inner graph in a context where no human can ever resume
        # it: the production deadlock the framework HITL contract prohibits.
        #
        # Deterministic non-HITL outcome: present every synthesized plan and
        # leave the selection UNMADE. Choosing among the 3 plans is the
        # organizer's judgement (docs/02_design.md §HITL設計 — 壁打ち対話が本
        # テンプレの中核価値); silently finalizing plans[0] would push an
        # unchosen plan downstream as if it had been approved. The routing
        # contract is preserved — refine_directive="approve" routes to END via
        # DomainWorkflowGraph.route_after_hitl_refine().
        if not state.get("hitl_allowed", True):
            emit_trace_event(
                "hitl_skipped_not_allowed",
                {"reason": "hitl_allowed=false", "plans_count": len(plans)},
                state,
            )
            return {
                # hitl_feedback / hitl_status are framework-written fields
                # (framework HITL schema 1.4) — the template writes only
                # hitl_draft. No human responded, so neither is set here.
                "hitl_draft": draft,
                "refine_directive": "approve",
                "selected_plan": {},
                "quality_notes": list(state.get("quality_notes", []))
                + [_HITL_SKIPPED_NOTE],
                "status": AgentStatus.SUCCESS.value,
            }

        raw_feedback = interrupt({
            "draft": draft,
            "reason": "corporate_trip_plan_review",
            "prompt": "3案から選択、または条件変更（予算/日程/宿泊タイプ等）をご記入ください。",
        })

        text = self._extract_text(raw_feedback)
        sanitized_text, node_redaction_count = sanitize_identifiers(text)
        ingress_count = trusted_redaction_count(
            raw_feedback, INGRESS_REDACTION_COUNT_KEY
        )
        # Direct node callers may bypass the graph ingress.  A measurement of
        # the raw text must win in that case; for normal graph traffic the
        # text is already clean and the ingress-only internal count is used.
        # Do not take the larger value: that would make an injected count an
        # audit-log overstatement.
        redaction_count = (
            node_redaction_count
            if node_redaction_count
            else ingress_count
        )

        if redaction_count > 0:
            emit_trace_event("hitl_response_sanitized", {"redaction_count": redaction_count}, state)

        directive, notes = self._interpret(sanitized_text)

        emit_trace_event("hitl_resumed", {"directive": directive}, state)

        result: dict = {
            "hitl_draft": draft,
            "hitl_feedback": sanitized_text,
            "refine_directive": directive,
            "status": AgentStatus.SUCCESS.value,
        }
        if directive == "approve":
            index = self._select_plan_index(sanitized_text, len(plans))
            result["selected_plan"] = plans[index] if plans else {}
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _extract_text(raw_feedback) -> str:
        if raw_feedback is None:
            return ""
        if isinstance(raw_feedback, dict):
            return raw_feedback.get("text", "") or raw_feedback.get("response", "")
        return str(raw_feedback)

    @staticmethod
    def _select_plan_index(sanitized_text: str, plan_count: int) -> int:
        """Parses which of the N presented plans the organizer approved.

        review finding, this session: approval previously always
        finalized plans[0] even though the prompt explicitly asks the
        organizer to choose among 3 (see execute()'s interrupt() prompt).
        Falls back to index 0 only when no selection signal is present in
        the response (e.g. a bare "承認します" with no plan reference) —
        that is a legitimate "approve the (only/first) presented option"
        case, not a silent misfire.
        """
        if plan_count <= 1:
            return 0
        for pattern in _PLAN_INDEX_PATTERNS:
            m = pattern.search(sanitized_text)
            if m:
                idx = int(m.group(1)) - 1  # organizer-facing plans are 1-indexed
                if 0 <= idx < plan_count:
                    return idx
        for word, idx in _ORDINAL_WORDS.items():
            if word in sanitized_text and idx < plan_count:
                return idx
        return 0

    def _interpret(self, sanitized_text: str) -> tuple[str, str]:
        """Interprets the sanitized response into a refine_directive.

        new_search only fires on an EXPLICIT organizer request for a
        different destination/search axis (docs/02_design.md Cat 2 境界) —
        never as an autonomous re-search decision.
        """
        if not sanitized_text.strip():
            return "approve", ""

        lowered = sanitized_text.lower()
        if any(kw in sanitized_text or kw in lowered for kw in _NEW_SEARCH_KEYWORDS):
            return "new_search", sanitized_text[:200]

        if self._llm_client is not None:
            interpreted = self._llm_client.interpret_refine_response(sanitized_text)
            directive = interpreted.get("directive", "adjust")
            if directive not in ("approve", "adjust", "new_search"):
                directive = "adjust"
            return directive, interpreted.get("notes", sanitized_text[:200])

        # No LLM wired (dev/test default): any non-empty free text is treated
        # as an adjust request; explicit approval keywords short-circuit.
        if any(kw in lowered for kw in ("approve", "承認", "これで", "確定", "ok", "了解")):
            return "approve", sanitized_text[:200]
        return "adjust", sanitized_text[:200]

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
