"""AgentCore Platform v1.0 — CMN-C2-655 TripPlanWorkflowGraphNode.

docs/02_design.md §GraphNode契約（TripPlanWorkflowGraphNode）. Wraps the inner
DomainWorkflowGraph (src/graph/domain_workflow_graph.py); assigned to the
outer `main` slot.

Key contract points (verified against the actual 1.0.0 wheel
framework.nodes.graph_node.GraphNode implementation, not the earlier design
draft's assumptions):
  - get_subgraph() MUST return the SAME cached instance every call — the
    inner graph's compiled checkpointer must persist identically across
    invoke() and the later resume() (TourPlanningProcurementRoutingAgent precedent; a fresh instance
    on resume loses the suspended checkpoint and causes an infinite
    suspend-reinvoke loop).
  - extract_input() returns a str — the JSON envelope built by pre_process
    (state["validated_input"]). The inner graph's `dispatch` node
    json.loads()'s it back into request_params/mode/care_requirements.
  - merge_output() receives sub_result = inner DomainWorkflowGraph.get_output()
    and returns ONLY the outer state delta.
  - propagate_hitl = True: when the inner `hitl_refine` node calls
    interrupt(), GraphNode.execute() (framework, not overridden here) detects
    sub_result["status"] == "awaiting_human" and re-raises interrupt() at the
    outer boundary so the top-level AgentBaseGraph.invoke() suspends the
    OUTER checkpoint too and returns AWAITING_HUMAN to the caller.
  - error_strategy = "propagate" (default): fallback/degradation for
    Web-search/DB failures happens INSIDE inner nodes (success + quality_notes);
    only unexpected exceptions reach this GraphNode boundary, and those should
    fail fast (SubgraphError), never be silently swallowed.
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.graph_node import GraphNode


class TripPlanWorkflowGraphNode(GraphNode):
    """Wraps the inner mode-branching domain workflow graph."""

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = True

    def __init__(self, config: dict | None = None) -> None:
        super().__init__()
        self._config = config or {}
        self._subgraph = None  # lazily built, then cached — see get_subgraph()

    def get_subgraph(self):
        """Returns the cached inner graph instance (compile-once contract).

        Compiling here (not just instantiating) so the InMemorySaver
        checkpointer attached at compile() time is stable across the
        invoke() -> interrupt() -> resume() lifecycle of a single process.
        """
        if self._subgraph is None:
            from src.graph.domain_workflow_graph import DomainWorkflowGraph
            from langgraph.checkpoint.memory import InMemorySaver

            graph = DomainWorkflowGraph(config=self._inner_config())
            graph.compile(checkpointer=InMemorySaver())
            self._subgraph = graph
        return self._subgraph

    def extract_input(self, state: dict) -> str:
        """Pass the JSON envelope built by pre_process straight through."""
        return state.get("validated_input", state.get("user_input", ""))

    def merge_output(self, state: dict, sub_result: dict) -> dict:
        """Maps inner get_output() fields to the outer state delta.

        Includes hitl_draft/hitl_feedback/hitl_status per docs/02_design.md
        §GraphNode契約 (HITL 伝播時の必須写像 — omission loses HITL state at
        the outer boundary per TourPlanningProcurementRoutingAgent precedent).
        """
        delta = {
            "result": sub_result.get("output"),
            "status": sub_result.get("status"),
            "mode": sub_result.get("mode", state.get("mode")),
            "plan_id": sub_result.get("plan_id", state.get("plan_id", "")),
            "quality_notes": sub_result.get("quality_notes", []),
            "policy_status": sub_result.get("policy_status", state.get("policy_status")),
            "policy_citations": sub_result.get("policy_citations", state.get("policy_citations", [])),
        }
        for hitl_key in ("hitl_draft", "hitl_feedback", "hitl_status"):
            if hitl_key in sub_result:
                delta[hitl_key] = sub_result[hitl_key]
        return delta

    def _inner_config(self) -> dict:
        """Forward the keys the inner graph needs (LLM/web-search/plans_count/etc.)."""
        return dict(self._config)
