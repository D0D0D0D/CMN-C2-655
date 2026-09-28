"""AgentCore Platform v1.0 — CMN-C2-655 CorporateGroupTripPlannerAgent (outer graph).

Cat 2 — AgentBaseGraph (fixed 5-node backbone) + GraphNode in the `main`
slot wrapping the inner mode-branching DomainWorkflowGraph.
docs/02_design.md §Architecture Overview / §Position in AgentCore Architecture.

Pipeline (outer, fixed — NOT overridden):
  START -> initialize -> pre_process -> main(TripPlanWorkflowGraphNode)
        -> {route} -> post_process -> finalize -> END

add_edges() is intentionally NOT overridden — AgentBaseGraph wires the full
default pipeline (including HITL routing via state["hitl_status"], see
framework.graph.agent_base_graph.AgentBaseGraph.route()).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from framework.graph.agent_base_graph import AgentBaseGraph
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.trip_plan_workflow_graph_node import TripPlanWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State
from src.services.ingress_sanitizer import (
    sanitize_graph_ingress,
    sanitize_feedback_ingress,
)


class CorporateGroupTripPlannerGraph(AgentBaseGraph):
    """Outer backbone graph for CMN-C2-655 CorporateGroupTripPlannerAgent."""

    @property
    def name(self) -> str:
        return "CMN-C2-655"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects "initialize" and "finalize"
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = TripPlanWorkflowGraphNode(config=self._main_node_config())
        self._nodes["post_process"] = PostProcessNode()

    # BaseGraph persists initial State, and Command(resume=...), before any
    # node runs. These overrides are the PII boundary for every public path.
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

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.

    def _main_node_config(self) -> dict:
        """Forward the config keys the inner domain workflow graph needs.

        Includes the real-backend injection keys (`policy_db` /
        `trip_records_db` / `trip_kb_vector_store` / `trip_kb_embedding`) set by
        `src/api/server.py::_wire_real_services()` — omitting them here would
        silently strand the wired backends at the outer graph and leave the
        inner graph's services permanently in "not wired" degraded mode
        (review finding, this session).
        """
        return {
            "llm": self.config.get("llm", {}),
            "web_search": self.config.get("web_search", {}),
            "plans_count": self.config.get("plans_count", 3),
            "ingest": self.config.get("ingest", {}),
            "memory_enabled": self.config.get("memory_enabled", True),
            "hitl": self.config.get("hitl", {}),
            "policy_db": self.config.get("policy_db"),
            "trip_records_db": self.config.get("trip_records_db"),
            "trip_kb_vector_store": self.config.get("trip_kb_vector_store"),
            "trip_kb_embedding": self.config.get("trip_kb_embedding"),
        }
