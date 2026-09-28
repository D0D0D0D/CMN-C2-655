"""AgentCore Platform v1.0 — CMN-C2-655 DomainWorkflowGraph (inner graph).

docs/02_design.md §Architecture Overview 内側 / §Node Configuration — 内側.
Instantiated (and cached) by TripPlanWorkflowGraphNode.get_subgraph().

Topology (BaseGraph — fully custom, mode-branching):

  START -> dispatch --+-- [mode=ingest]   -> ingest_normalize -> ingest_store -> END
                       |
                       +-- [mode=plan]     -> policy_check -> web_search -> candidate_rank
                       |                       -> plan_synthesize -> hitl_refine --+-> END (approve)
                       |                          ^                    |            |
                       |                          | (adjust)  <--------+            |
                       |                          +-- (new_search, explicit) -> web_search
                       |
                       +-- [mode=feedback] -> feedback_update -> END

route() after dispatch branches on `mode`. route() after hitl_refine branches
on `refine_directive` — "approve" -> END, "adjust" -> plan_synthesize,
"new_search" -> web_search (explicit organizer request only, never automatic
— docs/02_design.md Cat 2 境界).

All node/route/get_output functions here are annotated with this module's
own `State` (never bare AgentState) — annotating with AgentState causes
LangGraph to project custom fields away and break routing (Business Travel Hotel & Plan Recommendation Agent
precedent cited in docs/02_design.md §State Definition note).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from src.nodes.candidate_rank_node import CandidateRankNode
from src.nodes.dispatch_node import DispatchNode
from src.nodes.feedback_update_node import FeedbackUpdateNode
from src.nodes.hitl_refine_node import HitlRefineNode
from src.nodes.ingest_normalize_node import IngestNormalizeNode
from src.nodes.ingest_store_node import IngestStoreNode
from src.nodes.plan_synthesize_node import PlanSynthesizeNode
from src.nodes.policy_check_node import PolicyCheckNode
from src.nodes.web_search_node import WebSearchNode
from src.schemas.state import State
from src.services.llm_client import TripPlannerLLMClient
from src.services.policy_service import PolicyService
from src.services.trip_kb_service import TripKBService
from src.services.trip_records_service import TripRecordsService
from src.services.web_search_service import WebSearchService
from src.services.ingress_sanitizer import (
    sanitize_graph_ingress,
    sanitize_feedback_ingress,
)


class DomainWorkflowGraph(BaseGraph):
    """Inner mode-branching domain workflow for CMN-C2-655."""

    @property
    def name(self) -> str:
        return "cmn_c2_655_trip_plan_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config keys — services degrade gracefully when unwired
        # (dev/test default; server.py wires real services at startup).
        pass

    # This graph also has a direct-invocation contract through GraphNode and
    # tests. Protect its independent initial State/checkpoint just as the
    # outer graph does.
    def invoke(
        self,
        user_input: str,
        session_id: str = "",
        ctx: Any | None = None,
        input_context: dict | None = None,
    ) -> dict:
        clean_user_input, clean_context = sanitize_graph_ingress(user_input, input_context)
        return super().invoke(
            clean_user_input,
            session_id=session_id,
            ctx=ctx,
            input_context=clean_context,
        )

    def resume(self, thread_id: str, feedback: Any) -> dict:
        return super().resume(thread_id, sanitize_feedback_ingress(feedback))

    def stream(self, user_input: str, session_id: str = "", input_context: dict | None = None) -> Iterator[dict]:
        clean_user_input, clean_context = sanitize_graph_ingress(user_input, input_context)
        yield from super().stream(
            clean_user_input,
            session_id=session_id,
            input_context=clean_context,
        )

    # ------------------------------------------------------------------
    def register_nodes(self) -> None:
        llm_client = self._build_llm_client()
        policy_service = self._build_policy_service()
        trip_records_service = self._build_trip_records_service()
        trip_kb_service = self._build_trip_kb_service()
        web_search_service = self._build_web_search_service()
        plans_count = int(self.config.get("plans_count", 3))

        self._nodes["dispatch"] = DispatchNode()
        self._nodes["ingest_normalize"] = IngestNormalizeNode(llm_client=llm_client)
        self._nodes["ingest_store"] = IngestStoreNode(
            policy_service=policy_service,
            trip_records_service=trip_records_service,
            trip_kb_service=trip_kb_service,
        )
        self._nodes["policy_check"] = PolicyCheckNode(policy_service=policy_service)
        self._nodes["web_search"] = WebSearchNode(
            web_search_service=web_search_service,
            trip_kb_service=trip_kb_service,
        )
        self._nodes["candidate_rank"] = CandidateRankNode(trip_kb_service=trip_kb_service)
        self._nodes["plan_synthesize"] = PlanSynthesizeNode(llm_client=llm_client, plans_count=plans_count)
        self._nodes["hitl_refine"] = HitlRefineNode(llm_client=llm_client)
        self._nodes["feedback_update"] = FeedbackUpdateNode(
            trip_records_service=trip_records_service,
            trip_kb_service=trip_kb_service,
            llm_client=llm_client,
        )

    def _build_llm_client(self) -> TripPlannerLLMClient | None:
        llm_config = self.config.get("llm")
        if not llm_config or not isinstance(llm_config, dict) or "instance" not in llm_config:
            return None
        return TripPlannerLLMClient(
            llm=llm_config["instance"],
            model=llm_config.get("model", "claude-sonnet"),
            temperature=llm_config.get("temperature", 0.2),
            max_tokens=llm_config.get("max_tokens", 4000),
        )

    def _build_policy_service(self) -> PolicyService | None:
        db = self.config.get("policy_db")
        return PolicyService(db) if db is not None else None

    def _build_trip_records_service(self) -> TripRecordsService | None:
        db = self.config.get("trip_records_db")
        return TripRecordsService(db) if db is not None else None

    def _build_trip_kb_service(self) -> TripKBService | None:
        vs = self.config.get("trip_kb_vector_store")
        embedding = self.config.get("trip_kb_embedding")
        return TripKBService(vs, embedding=embedding) if vs is not None else None

    def _build_web_search_service(self) -> WebSearchService:
        web_search_config = self.config.get("web_search", {}) or {}
        backend = web_search_config.get("backend")
        return WebSearchService(
            backend=backend,
            max_retries=web_search_config.get("max_retries", 2),
            timeout_seconds=web_search_config.get("timeout_seconds", 10),
        )

    # ------------------------------------------------------------------
    def add_edges(self) -> None:
        self._sg.add_edge(START, "dispatch")
        self._sg.add_conditional_edges("dispatch", self.route_after_dispatch)

        # ingest branch
        self._sg.add_edge("ingest_normalize", "ingest_store")
        self._sg.add_edge("ingest_store", END)

        # plan branch
        self._sg.add_edge("policy_check", "web_search")
        self._sg.add_edge("web_search", "candidate_rank")
        self._sg.add_edge("candidate_rank", "plan_synthesize")
        self._sg.add_edge("plan_synthesize", "hitl_refine")
        self._sg.add_conditional_edges("hitl_refine", self.route_after_hitl_refine)

        # feedback branch
        self._sg.add_edge("feedback_update", END)

    def route_after_dispatch(self, state: State) -> str:
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        mode = state.get("mode")
        if mode == "ingest":
            return "ingest_normalize"
        if mode == "feedback":
            return "feedback_update"
        return "policy_check"

    def route_after_hitl_refine(self, state: State) -> str:
        directive = state.get("refine_directive")
        if directive == "approve":
            return END
        if directive == "new_search":
            return "web_search"
        return "plan_synthesize"

    def route(self, state: State) -> str:
        """Required by BaseGraph ABC; not referenced by add_conditional_edges
        (route_after_dispatch / route_after_hitl_refine are used instead —
        two distinct branch points need two distinct routing functions)."""
        return END

    # ------------------------------------------------------------------
    def get_output(self, state: State) -> dict:
        """Shapes sub_result for TripPlanWorkflowGraphNode.merge_output()."""
        output: dict = {
            "output": self._build_output_payload(state),
            "status": state.get("status"),
            "mode": state.get("mode"),
            "plan_id": state.get("plan_id", ""),
            "quality_notes": state.get("quality_notes", []),
            "policy_status": state.get("policy_status"),
            "policy_citations": state.get("policy_citations", []),
        }
        # HITL propagation: when suspended, LangGraph's own __interrupt__
        # short-circuits get_output() before this point is reached at the
        # framework layer (BaseGraph.invoke()); on a normal (non-suspended)
        # return we still forward hitl_status/draft/feedback if present so
        # TripPlanWorkflowGraphNode.merge_output() can propagate them.
        for hitl_key in ("hitl_draft", "hitl_feedback", "hitl_status"):
            if hitl_key in state:
                output[hitl_key] = state[hitl_key]
        return output

    @staticmethod
    def _build_output_payload(state: State) -> dict:
        mode = state.get("mode")
        if mode == "ingest":
            return {
                "ingested_count": state.get("ingest_result", {}).get("ingested_count", 0),
                "skipped_count": state.get("ingest_result", {}).get("skipped_count", 0),
                "policy_version": state.get("ingest_result", {}).get("policy_version", ""),
                "ingest_summary": state.get("ingest_summary", {}),
            }
        if mode == "feedback":
            return state.get("feedback_result", {})
        return {
            "plans": state.get("plans", []),
            "selected_plan": state.get("selected_plan", {}),
            "policy_status": state.get("policy_status"),
            "policy_citations": state.get("policy_citations", []),
            "care_requirements": state.get("care_requirements", []),
            "assumption_notes": state.get("assumption_notes", []),
            "quality_notes": state.get("quality_notes", []),
        }
