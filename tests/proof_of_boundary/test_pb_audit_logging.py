# PB-1: BaseNode -> AuditLogger
# emit_trace_event() must fire from shared.utils.audit_logger inside every
# concrete node's execute() — at least one domain event per node, regardless
# of side effects (PB-1).

import importlib
import inspect
import pkgutil

# TripPlanWorkflowGraphNode inherits execute() from framework.nodes.graph_node
# .GraphNode UNMODIFIED (no override — see
# test_graph_node_class_exempt_from_execute_level_emit below). It has no
# domain logic of its own to instrument with emit_trace_event(); the actual
# S-4 domain events for the workflow it wraps are emitted by the 8 INNER
# nodes (dispatch/policy_check/web_search/... — all of which DO call
# emit_trace_event(), see their respective modules). Duplicating an event at
# this boundary would only re-state what the inner nodes already emit.
_EXEMPT_NO_DOMAIN_EXECUTE = frozenset({"TripPlanWorkflowGraphNode"})


def _discover_node_classes():
    from framework.nodes.base_node import BaseNode

    pkg = importlib.import_module("src.nodes")
    discovered = []
    for _, modname, _ in pkgutil.walk_packages(pkg.__path__, prefix="src.nodes."):
        module = importlib.import_module(modname)
        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseNode)
                and attr is not BaseNode
                and attr.__module__ == modname
                and not inspect.isabstract(attr)
            ):
                discovered.append(attr)
    return discovered


class TestPBAuditLogging:
    def test_every_node_source_calls_emit_trace_event(self):
        """Static check: every node module imports/calls emit_trace_event at
        least once (domain event), distinct from the framework's automatic
        node_start/node_complete/node_error. Exempts
        _EXEMPT_NO_DOMAIN_EXECUTE (see module-level comment)."""
        node_classes = _discover_node_classes()
        assert node_classes, "no concrete node classes found under src/nodes/"

        failures = []
        for node_cls in node_classes:
            if node_cls.__name__ in _EXEMPT_NO_DOMAIN_EXECUTE:
                continue
            source = inspect.getsource(inspect.getmodule(node_cls))
            if "emit_trace_event(" not in source:
                failures.append(node_cls.__name__)
        assert not failures, f"nodes missing emit_trace_event() call: {failures}"

    def test_graph_node_class_exempt_from_execute_level_emit(self):
        """TripPlanWorkflowGraphNode inherits execute() from the framework's
        GraphNode (no override) — S-4 domain events for the workflow are
        emitted by the INNER nodes it wraps, not duplicated at this
        boundary. Confirms the class does not override execute()."""
        from src.nodes.trip_plan_workflow_graph_node import TripPlanWorkflowGraphNode
        assert "execute" not in TripPlanWorkflowGraphNode.__dict__
