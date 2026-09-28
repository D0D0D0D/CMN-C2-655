"""AgentCore Platform v1.0 — CMN-C2-655 WebSearchNode.

docs/02_design.md §Node Configuration — 内側: `web_search`.

Fetches destination x season x budget lodging/transport/cost information at
invoke time (public info is fetched dynamically, not pre-built as a venue
DB — moat policy). On failure/unavailable backend (see
src/services/web_search_service.py — the framework's shared.tools.web_search
is a placeholder, confirmed at MR-B time), falls back to past-trip KB data
and annotates `quality_notes` — never silently degrades (docs/02_design.md
Usability §5).
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.trip_kb_service import TripKBService
from src.services.web_search_service import WebSearchService


class WebSearchNode(FunctionNode):
    """Fetches public destination/lodging/cost info; KB-fallback on failure."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, web_search_service: WebSearchService | None = None,
                 trip_kb_service: TripKBService | None = None) -> None:
        super().__init__()
        self._web_search_service = web_search_service or WebSearchService()
        self._trip_kb_service = trip_kb_service

    def execute(self, state: dict) -> dict:
        params = state.get("request_params", {})
        quality_notes = list(state.get("quality_notes", []))

        search_result = self._web_search_service.search_destination_options(
            destination_hint=params.get("destination_hint", ""),
            season=params.get("season", "unspecified"),
            budget_band=params.get("budget_band", "standard"),
        )

        if search_result["ok"]:
            emit_trace_event("web_search_completed", {
                "result_count": len(search_result["results"]),
            }, state)
            return {
                "web_results": search_result["results"],
                "web_search_ok": True,
                "quality_notes": quality_notes,
                "status": AgentStatus.SUCCESS.value,
            }

        # Fallback: past-trip KB realized data instead of live web results.
        emit_trace_event("web_search_fallback", {"error": search_result.get("error", "")}, state)
        quality_notes.append(
            "Web検索が利用できなかったため、相場情報は過去の旅行実績データに基づいています。"
        )
        fallback_results = []
        if self._trip_kb_service is not None:
            fallback_results = self._trip_kb_service.find_similar_trips(
                destination_hint=params.get("destination_hint", ""),
                season=params.get("season", "unspecified"),
                party_size=params.get("party_size") or 0,
                budget_band=params.get("budget_band", "standard"),
            )
        return {
            "web_results": fallback_results,
            "web_search_ok": False,
            "quality_notes": quality_notes,
            "status": AgentStatus.SUCCESS.value,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
