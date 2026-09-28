# CMN-C2-655 — Unit Tests: WebSearchNode
# Covers the fallback-to-KB path (docs/02_design.md Web検索失敗時) and confirms
# the framework's shared.tools.web_search placeholder is correctly treated as
# "unavailable" rather than trusted as real market data (MR-B finding — see
# src/services/web_search_service.py module docstring).

from framework.schemas.agent_status import AgentStatus
from src.nodes.web_search_node import WebSearchNode
from src.services.web_search_service import WebSearchService, WebSearchUnavailableError


class _AlwaysFailBackend:
    def search(self, query: str) -> str:
        raise WebSearchUnavailableError("no backend configured")


class _WorkingBackend:
    def search(self, query: str) -> str:
        return f"real results for {query}"


class _FakeTripKB:
    def __init__(self, matches=None):
        self._matches = matches or []

    def find_similar_trips(self, **kwargs):
        return self._matches


class TestWebSearchNodeFallback:
    def test_placeholder_backend_falls_back_to_kb(self):
        service = WebSearchService(backend=_AlwaysFailBackend())
        kb = _FakeTripKB(matches=[{"metadata": {"destination_area": "Hakone", "satisfaction_score": 4.2}}])
        node = WebSearchNode(web_search_service=service, trip_kb_service=kb)
        state = {"request_params": {"destination_hint": "Hakone", "season": "spring", "budget_band": "standard"},
                 "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
                 "node_history": [], "error_log": []}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["web_search_ok"] is False
        assert len(result["web_results"]) == 1
        assert any("過去の旅行実績データ" in n for n in result["quality_notes"])

    def test_default_backend_detects_framework_placeholder(self):
        # Uses the REAL default backend (PlaceholderDetectedBackend wrapping
        # shared.tools.web_search) — confirms it does not trust the
        # framework's fixed placeholder string as real data.
        service = WebSearchService()
        node = WebSearchNode(web_search_service=service, trip_kb_service=None)
        state = {"request_params": {"destination_hint": "Hakone", "season": "spring", "budget_band": "standard"},
                 "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
                 "node_history": [], "error_log": []}
        result = node.execute(state)
        assert result["web_search_ok"] is False


class TestWebSearchNodeSuccess:
    def test_working_backend_returns_ok(self):
        service = WebSearchService(backend=_WorkingBackend())
        node = WebSearchNode(web_search_service=service, trip_kb_service=None)
        state = {"request_params": {"destination_hint": "Hakone", "season": "spring", "budget_band": "standard"},
                 "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
                 "node_history": [], "error_log": []}
        result = node.execute(state)
        assert result["web_search_ok"] is True
        assert len(result["web_results"]) == 1
