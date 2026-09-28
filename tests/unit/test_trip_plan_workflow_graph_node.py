# CMN-C2-655 — Unit Tests: TripPlanWorkflowGraphNode
# Confirms the GraphNode subgraph-caching contract (docs/02_design.md
# §GraphNode契約) — a fresh instance on every get_subgraph() call would break
# HITL resume (TourPlanningProcurementRoutingAgent precedent).

from src.nodes.trip_plan_workflow_graph_node import TripPlanWorkflowGraphNode


class TestGetSubgraphCaching:
    def test_get_subgraph_returns_same_instance(self):
        node = TripPlanWorkflowGraphNode(config={})
        first = node.get_subgraph()
        second = node.get_subgraph()
        assert first is second


class TestExtractInputMergeOutput:
    def test_extract_input_passes_validated_input(self):
        node = TripPlanWorkflowGraphNode(config={})
        state = {"validated_input": '{"mode": "plan"}', "user_input": "raw"}
        assert node.extract_input(state) == '{"mode": "plan"}'

    def test_extract_input_falls_back_to_user_input(self):
        node = TripPlanWorkflowGraphNode(config={})
        state = {"user_input": "raw"}
        assert node.extract_input(state) == "raw"

    def test_merge_output_maps_fields(self):
        node = TripPlanWorkflowGraphNode(config={})
        sub_result = {"output": {"markdown": "x"}, "status": "success", "mode": "plan",
                      "plan_id": "p1", "quality_notes": ["note"], "policy_status": "verified",
                      "policy_citations": []}
        delta = node.merge_output({}, sub_result)
        assert delta["result"] == {"markdown": "x"}
        assert delta["plan_id"] == "p1"
        assert delta["status"] == "success"

    def test_merge_output_propagates_hitl_fields(self):
        node = TripPlanWorkflowGraphNode(config={})
        sub_result = {"output": None, "status": "awaiting_human",
                      "hitl_draft": ["plan1"], "hitl_feedback": None, "hitl_status": None}
        delta = node.merge_output({}, sub_result)
        assert delta["hitl_draft"] == ["plan1"]
        assert "hitl_feedback" in delta
        assert "hitl_status" in delta

    def test_error_strategy_and_propagate_hitl_flags(self):
        assert TripPlanWorkflowGraphNode.error_strategy == "propagate"
        assert TripPlanWorkflowGraphNode.propagate_hitl is True
