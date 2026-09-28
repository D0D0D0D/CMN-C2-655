"""AgentCore Platform v1.0 — CMN-C2-655 PostProcessNode.

docs/02_design.md §Node Configuration — 外側（backbone 3スロット）: `post_process`.
docs/02_design.md §Usability設計判断 #2: 稟議・社内回覧を想定した Markdown 企画書
（行程表・費用概算表・規程チェック結果〔条項引用付き〕・参加者対応必要事項・期限付き
幹事タスク）＋ 機械可読 JSON の 2 形式を `output` に組み立てる。

Fixed line requirement (Usability §2, user decision 2026-07-06):
幹事タスク欄に固定文言「配慮対象者の個別対応は名簿と突き合わせて実施（AG は個人紐付けを
保持しない）」を必ず1行含める（匿名化集計方式の運用前提の明示）。
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.services.identifier_sanitizer import find_blocking_identifiers
from shared.utils.audit_logger import emit_trace_event

_CARE_TASK_FIXED_LINE = (
    "配慮対象者の個別対応は名簿と突き合わせて実施（AG は個人紐付けを保持しない）"
)

# Closed allowlist of plan fields that may leave the agent (review
# 2026-07-29, High). plan_synthesize's dicts also carry `rationale` (LLM free
# text) and `care_accommodations` (per-plan care detail) — neither is rendered
# in the Markdown, and emitting them verbatim in the JSON would (a) widen the
# C-01 surface, since care information may leave the agent only as the
# anonymized `care_requirements` aggregate, and (b) ship unreviewed LLM prose
# straight to the caller. A denylist would silently pass whatever new key a
# future plan_synthesize revision adds, so this stays an allowlist.
_PLAN_PUBLIC_FIELDS = (
    "destination_area",
    "itinerary",
    "lodging",
    "transport",
    "estimated_cost_per_person",
)


def _project_plan(plan: dict) -> dict:
    """Return only the allowlisted, caller-facing fields of a plan dict."""
    if not isinstance(plan, dict):
        return {}
    return {key: plan[key] for key in _PLAN_PUBLIC_FIELDS if key in plan}


# Depth ceiling for the S-3 output walk. The real payload nests 3 levels
# (formatted_output -> json -> candidate_plans[] -> itinerary[]); anything
# beyond this is malformed, and an unbounded walk that blows the stack is a
# gate that did not run.
_MAX_OUTPUT_DEPTH = 20


def _collect_output_strings(value, *, _depth: int = 0, _seen: frozenset = frozenset()) -> "list[str]":
    """Return every string in a JSON-compatible payload — fail closed on the rest.

    The framework's @final S-3 only inspects top-level result strings, so a
    nested payload reaches the caller unscanned unless the template walks it.
    Everything this walker cannot prove it understands raises instead of being
    skipped: a `set` or a custom object would otherwise slip past the gate and
    still be serialised by FastAPI's jsonable_encoder (review 2026-07-29,
    High). Non-string dict keys are rejected for the same reason — they are not
    JSON-serialisable, so their presence means the payload is not what the gate
    thinks it is. Cycles and excessive depth are errors, not silent truncation.
    """
    if _depth > _MAX_OUTPUT_DEPTH:
        raise RuntimeError(
            f"S-3 output gate: formatted output nests deeper than {_MAX_OUTPUT_DEPTH} "
            "levels; refusing to emit an unscanned payload."
        )
    # bool is a subclass of int — both are scalars carrying no text.
    if value is None or isinstance(value, (bool, int, float)):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (dict, list, tuple)):
        if id(value) in _seen:
            raise RuntimeError(
                "S-3 output gate: cyclic structure in formatted output; "
                "refusing to emit an unscanned payload."
            )
        seen = _seen | {id(value)}
        found: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise RuntimeError(
                        f"S-3 output gate: non-string key {type(key).__name__} in formatted "
                        "output; refusing to emit a payload that is not JSON-serialisable."
                    )
                found.append(key)
                found.extend(_collect_output_strings(item, _depth=_depth + 1, _seen=seen))
        else:
            for item in value:
                found.extend(_collect_output_strings(item, _depth=_depth + 1, _seen=seen))
        return found
    raise RuntimeError(
        f"S-3 output gate: non-JSON-serialisable value of type {type(value).__name__} "
        "in formatted output; refusing to emit an unscanned payload."
    )


_CARE_SURVEY_TEMPLATE = (
    "## 配布用配慮アンケート（テンプレート）\n\n"
    "- ご参加にあたり配慮が必要な事項があればお知らせください（食事制限・アレルギー・"
    "移動手段など。個人が特定できる詳細は幹事にのみ個別にお伝えください）。\n"
    "  - [ ] ベジタリアン / ヴィーガン対応が必要\n"
    "  - [ ] ハラール対応が必要\n"
    "  - [ ] アレルギー対応が必要（品目は個別申告）\n"
    "  - [ ] 車椅子・移動配慮が必要\n"
    "  - [ ] その他（幹事へ個別連絡）\n"
)


class PostProcessNode(FunctionNode):
    """Formats the final Markdown + JSON output package."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        # main (GraphNode) passes through upstream error status untouched —
        # post_process must not mask an error as success.
        if state.get("status") == AgentStatus.ERROR.value:
            return {"status": AgentStatus.ERROR.value}

        mode = state.get("mode", "plan")
        result = state.get("result") or {}

        if mode == "ingest":
            formatted = self._format_ingest(result)
        elif mode == "feedback":
            formatted = self._format_feedback(result)
        else:
            formatted = self._format_plan(state, result)

        emit_trace_event("post_process_formatted", {"mode": mode}, state)
        return {
            "formatted_output": formatted,
            "status": AgentStatus.SUCCESS.value,
        }

    # ------------------------------------------------------------------
    def _format_plan(self, state: dict, result: dict) -> dict:
        plans = result.get("plans") or []
        selected = result.get("selected_plan") or {}
        # Selection-pending path: when hitl_allowed is
        # false the organizer never chose among the synthesized plans, so
        # selected_plan is deliberately empty. Falling back to plans[0] here —
        # as this method previously did unconditionally — would present an
        # unchosen plan as THE itinerary, quietly turning "selection unmade"
        # back into "plans[0] approved". Present every candidate instead and
        # say plainly that the selection is outstanding.
        #
        # Any plan count triggers this — "nobody chose" is independent of how
        # many candidates exist, and a single unconfirmed plan is exactly as
        # unchosen as three. (With no LLM wired, plan_synthesize emits one
        # placeholder plan; in production it emits plans_count=3.)
        #
        # Three states, not two: a plan was chosen / plans exist but none was
        # chosen / synthesis produced nothing. Collapsing the last into
        # "selected" would emit an 行程表 heading and selection_status="selected"
        # over an empty plan (review 2026-07-29, Medium).
        selection_pending = not selected and bool(plans)
        no_candidates = not selected and not plans
        plan = selected or (plans[0] if plans else {})
        policy_citations = state.get("policy_citations", []) or result.get("policy_citations", [])
        policy_status = state.get("policy_status", result.get("policy_status", "unverified"))
        care_requirements = state.get("care_requirements", []) or result.get("care_requirements", [])
        assumption_notes = state.get("assumption_notes", []) or result.get("assumption_notes", [])
        quality_notes = state.get("quality_notes", []) or result.get("quality_notes", [])
        plan_id = state.get("plan_id") or result.get("plan_id", "")

        lines = ["# 社員旅行企画書（ドラフト）", ""]
        if assumption_notes:
            lines.append("## 前提（未入力項目の既定値）")
            for note in assumption_notes:
                lines.append(f"- {note}")
            lines.append("")

        if selection_pending:
            lines.append(f"## 候補プラン（{len(plans)} 案・幹事による選定が未了）")
            lines.append(
                "- ⚠️ 幹事との対話が行われていないため、いずれの案も確定していません。"
                "下記から選定のうえ条件をご調整ください。"
            )
            for i, candidate in enumerate(plans, 1):
                lines.append(f"### 案{i}: {candidate.get('destination_area', '（未確定）')}")
                for day in candidate.get("itinerary", []) or ["（詳細は選定後に確定）"]:
                    lines.append(f"  - {day}")
                lines.append(f"  - 宿泊: {candidate.get('lodging', '（未確定）')}")
                lines.append(f"  - 交通: {candidate.get('transport', '（未確定）')}")
                lines.append(
                    f"  - 一人当たり概算費用: "
                    f"{candidate.get('estimated_cost_per_person', '（未確定）')}"
                )
            lines.append("")
        elif no_candidates:
            lines.append("## 候補プラン")
            lines.append(
                "- ⚠️ 提示できる旅程プランを生成できませんでした。"
                "入力条件（人数・予算感・時期・出発地）をご確認のうえ再実行してください。"
            )
            lines.append("")
        else:
            lines.append("## 行程表")
            lines.append(f"- 目的地エリア: {plan.get('destination_area', '（未確定）')}")
            for day in plan.get("itinerary", []) or ["（詳細は HITL 確定後に反映）"]:
                lines.append(f"  - {day}")
            lines.append(f"- 宿泊: {plan.get('lodging', '（未確定）')}")
            lines.append(f"- 交通: {plan.get('transport', '（未確定）')}")
            lines.append("")

            lines.append("## 費用概算")
            lines.append(f"- 一人当たり概算費用: {plan.get('estimated_cost_per_person', '（未確定）')}")
            lines.append("")

        lines.append("## 規程チェック結果")
        lines.append(f"- ステータス: {policy_status}")
        if policy_status == "unverified":
            lines.append("- ⚠️ 規程が未整備のため自動チェックできませんでした。総務にご確認ください。")
        for citation in policy_citations:
            lines.append(
                f"- {citation.get('policy_version', '')} 第{citation.get('clause_no', '?')}条: "
                f"{citation.get('excerpt', '')}"
            )
        lines.append("")

        lines.append("## 参加者対応必要事項（匿名化集計）")
        if care_requirements:
            for c in care_requirements:
                lines.append(f"- {c.get('category')}: {c.get('count')} 名")
        else:
            lines.append("- 現時点で申告された配慮事項はありません。")
        lines.append("")

        lines.append("## 幹事タスク（期限付き）")
        lines.append("- [ ] 宿泊先の仮予約（〆切: プラン確定後3営業日以内）")
        lines.append("- [ ] 参加者への配慮アンケート配布・回収（下記テンプレート使用）")
        lines.append(f"- [ ] {_CARE_TASK_FIXED_LINE}")
        lines.append("")

        if quality_notes:
            lines.append("## 品質注記")
            for note in quality_notes:
                lines.append(f"- {note}")
            lines.append("")

        lines.append(_CARE_SURVEY_TEMPLATE)

        markdown = "\n".join(lines)

        if selection_pending:
            selection_status = "pending_organizer"
        elif no_candidates:
            selection_status = "no_candidates"
        else:
            selection_status = "selected"

        return {
            "markdown": markdown,
            "json": {
                "plan_id": plan_id,
                # Machine consumers must be able to tell "the organizer picked
                # this" from "nobody has picked yet" — see selection_pending above.
                # Both sides go through _project_plan(): the JSON must not carry
                # fields the Markdown never shows (rationale / care_accommodations).
                "plan": {} if selection_pending else _project_plan(plan),
                "candidate_plans": [_project_plan(p) for p in plans] if selection_pending else [],
                "selection_status": selection_status,
                "policy_status": policy_status,
                "policy_citations": policy_citations,
                "care_requirements": care_requirements,
                "assumption_notes": assumption_notes,
                "quality_notes": quality_notes,
            },
        }

    def _format_ingest(self, result: dict) -> dict:
        summary = result.get("ingest_summary", {})
        markdown = (
            "# 取込結果\n\n"
            f"- 取込件数: {result.get('ingested_count', 0)}\n"
            f"- スキップ件数: {result.get('skipped_count', 0)}\n"
            f"- 規程版番号: {result.get('policy_version', '（該当なし）')}\n"
        )
        return {"markdown": markdown, "json": {**result, "ingest_summary": summary}}

    def _format_feedback(self, result: dict) -> dict:
        markdown = (
            "# フィードバック受理結果\n\n"
            f"- plan_id: {result.get('plan_id', '')}\n"
            f"- 受理: {result.get('updated', False)}\n"
        )
        return {"markdown": markdown, "json": result}

    # ------------------------------------------------------------------
    def _extra_security_gate_output(self, result: dict) -> dict:
        """S-3 extension: scan final output for residual direct identifiers.

        Domain-specific credential re-check is handled by the default
        FunctionNode S-3 scan (_security_gate_output, @final) already; here we
        add the direct-identifier residual scan required by C-01.

        Scans the WHOLE formatted payload — Markdown *and* every string nested
        in the JSON side. The framework's @final S-3 only walks top-level
        result strings, so a nested dict (e.g. candidate_plans entries) would
        otherwise leave the agent unscanned (review 2026-07-29, High).
        _project_plan()'s allowlist is the primary defence — it bounds which
        KEYS may leave. This walk is the backstop for their VALUES, which are
        LLM-authored and unbounded: an allowlisted `lodging` reading
        「田中太郎さん向け」 is exactly what it exists to catch.
        """
        formatted = result.get("formatted_output")
        if formatted is not None:
            for text in _collect_output_strings(formatted):
                labels = find_blocking_identifiers(text)
                if labels:
                    raise RuntimeError(
                        "S-3 output gate: residual direct identifier "
                        f"({labels[0]}) detected in formatted output. "
                        "Blocked per C-01 (participant care info must be anonymized aggregate only)."
                    )
        return result
