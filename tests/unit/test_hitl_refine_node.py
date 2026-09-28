# CMN-C2-655 — Unit Tests: HitlRefineNode
# Full interrupt()/resume() lifecycle is covered by
# tests/integration/test_hitl_flow.py (PB-7 + resume->success + sanitization
# PB). These unit tests isolate the pure interpretation/sanitization logic.

import src.nodes.hitl_refine_node as hitl_refine_node_module
from src.nodes.hitl_refine_node import HitlRefineNode


class TestHitlRefineNodeInterpretation:
    def setup_method(self):
        self.node = HitlRefineNode(llm_client=None)

    def test_empty_response_approves(self):
        directive, _ = self.node._interpret("")
        assert directive == "approve"

    def test_approval_keyword_approves(self):
        directive, _ = self.node._interpret("承認します")
        assert directive == "approve"

    def test_new_search_keyword_explicit(self):
        directive, _ = self.node._interpret("別の目的地を探してください")
        assert directive == "new_search"

    def test_generic_adjust_request(self):
        directive, _ = self.node._interpret("予算をもう少し下げてください")
        assert directive == "adjust"


class TestHitlRefineNodeSanitizationIntegration:
    """Confirms the node calls the shared sanitizer and never stores raw text."""

    def setup_method(self):
        self.node = HitlRefineNode(llm_client=None)

    def test_execute_sanitizes_before_interpreting(self, monkeypatch):
        # Monkeypatch langgraph.types.interrupt (imported into the node module)
        # to simulate a resume delivering a raw identifier-laden response,
        # without needing a real compiled graph.
        raw_response = "田中太郎さんはベジタリアンなので和食にしてください。"

        def fake_interrupt(payload):
            return raw_response

        monkeypatch.setattr(hitl_refine_node_module, "interrupt", fake_interrupt)

        events = []
        monkeypatch.setattr(
            hitl_refine_node_module, "emit_trace_event",
            lambda event_type, payload, state: events.append((event_type, payload)),
        )

        state = {
            "plans": [{"destination_area": "Hakone"}],
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = self.node.execute(state)

        # The raw name must NEVER appear in the node's returned hitl_feedback.
        assert "田中太郎" not in result["hitl_feedback"]
        assert "[識別子除去済み]" in result["hitl_feedback"]

        sanitize_events = [p for t, p in events if t == "hitl_response_sanitized"]
        assert len(sanitize_events) == 1
        assert sanitize_events[0]["redaction_count"] >= 1
        # Payload must carry ONLY the count, never the removed content.
        assert set(sanitize_events[0].keys()) == {"redaction_count"}
