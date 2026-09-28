"""TC-06 / TC-07: FunctionNode security gates must not be bypassable.

These framework-compliance tests are required for every template that has one or
more FunctionNode subclasses. They validate the runtime enforcement boundary:
domain nodes must extend S-2/S-3 only through _extra_security_gate_input() and
_extra_security_gate_output(), never by replacing the default gates.
"""

import pytest

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel


class TestFunctionNodeFinalSecurityGates:
    """TC-06/TC-07: FunctionNode security gates are non-bypassable."""

    def test_tc06_security_gate_input_cannot_be_overridden(self):
        """TC-06: overriding the S-2 default input gate raises at class definition."""
        with pytest.raises(TypeError, match="_security_gate_input"):

            class _InvalidInputGateOverride(FunctionNode):
                required_trust_level = TrustLevel.ANONYMOUS

                def _security_gate_input(self, state):
                    return state

                def execute(self, state):
                    return {"status": AgentStatus.SUCCESS.value}

    def test_tc07_security_gate_output_cannot_be_overridden(self):
        """TC-07: overriding the S-3 default output gate raises at class definition."""
        with pytest.raises(TypeError, match="_security_gate_output"):

            class _InvalidOutputGateOverride(FunctionNode):
                required_trust_level = TrustLevel.ANONYMOUS

                def _security_gate_output(self, result):
                    return result

                def execute(self, state):
                    return {"status": AgentStatus.SUCCESS.value}


class TestCMNC2655GatesExtendNotReplace:
    """Template-specific companion: this agent's own nodes must extend the
    gates through the hooks, never by replacing the @final defaults."""

    def test_post_process_extends_output_gate_via_hook(self):
        from src.nodes.post_process_node import PostProcessNode

        # The hook is the template's; the @final gate stays the framework's.
        assert "_extra_security_gate_output" in vars(PostProcessNode)
        assert "_security_gate_output" not in vars(PostProcessNode)
        assert PostProcessNode._security_gate_output is FunctionNode._security_gate_output

    def test_every_function_node_subclass_declares_trust_level(self):
        """TC-08 companion — implicit ANONYMOUS inheritance is not acceptable."""
        import importlib
        import pkgutil

        import src.nodes as nodes_pkg

        offenders = []
        for mod in pkgutil.iter_modules(nodes_pkg.__path__):
            module = importlib.import_module(f"src.nodes.{mod.name}")
            for name, obj in vars(module).items():
                if (isinstance(obj, type) and issubclass(obj, FunctionNode)
                        and obj is not FunctionNode and obj.__module__ == module.__name__
                        and "required_trust_level" not in vars(obj)):
                    offenders.append(f"{module.__name__}.{name}")
        assert not offenders, f"missing required_trust_level: {offenders}"
