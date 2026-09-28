"""AgentCore Platform v1.0 — CMN-C2-655 CorporateGroupTripPlannerAgent state."""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# docs/02_design.md §State Definition is the source of truth for this schema.
# This single `State` is shared by BOTH the outer AgentBaseGraph (backbone) and
# the inner DomainWorkflowGraph (mode-branching custom topology) — the inner
# graph's dispatch/route/get_output functions are annotated with this same
# `State` type (never the bare AgentState) so LangGraph does not project away
# these custom fields (Business Travel Hotel & Plan Recommendation Agent precedent, see docs/02_design.md note).
#
# C-01 (mandatory): participant care/accommodation information is stored ONLY
# as anonymized aggregates (category x headcount). No node return value may
# carry a name<->care-need linkage. See policy in each node touching
# `care_requirements` / `complaint_flags`.

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for CorporateGroupTripPlannerAgent (Cat 2, HITL-enabled).

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    """

    # ── mode dispatch ────────────────────────────────────────────────────────
    mode: NotRequired[str]  # "ingest" / "plan" / "feedback"

    # ── pre_process -> main (outer) / dispatch (inner hydration) ────────────
    # JSON string envelope produced by pre_process, consumed by
    # TripPlanWorkflowGraphNode.extract_input() and json.loads'd by the inner
    # `dispatch` node into request_params/mode/care_requirements.
    request_params: NotRequired[dict]
    assumption_notes: NotRequired[list]

    # ── policy_check ─────────────────────────────────────────────────────────
    policy_constraints: NotRequired[list]
    policy_citations: NotRequired[list]
    policy_status: NotRequired[str]  # "verified" / "unverified"

    # ── care / participant considerations (anonymized aggregate ONLY, C-01) ──
    care_requirements: NotRequired[list]  # [{"category": str, "count": int}, ...]

    # ── web_search ────────────────────────────────────────────────────────────
    web_results: NotRequired[list]
    web_search_ok: NotRequired[bool]
    quality_notes: NotRequired[list]

    # ── candidate_rank ────────────────────────────────────────────────────────
    kb_matches: NotRequired[list]
    ranked_candidates: NotRequired[list]

    # ── plan_synthesize / hitl_refine ────────────────────────────────────────
    plans: NotRequired[list]
    selected_plan: NotRequired[dict]
    refine_directive: NotRequired[str]  # "approve" / "adjust" / "new_search"

    # ── shared plan identity ─────────────────────────────────────────────────
    plan_id: NotRequired[str]

    # ── ingest mode ───────────────────────────────────────────────────────────
    normalized_records: NotRequired[list]
    ingest_summary: NotRequired[dict]
    ingest_result: NotRequired[dict]

    # ── feedback mode ─────────────────────────────────────────────────────────
    feedback_result: NotRequired[dict]
