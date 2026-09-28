"""AgentCore Platform v1.0 — CMN-C2-655 Web search injection seam.

Web search is used to fetch destination x season x budget lodging/transport/
sightseeing/cost-of-living information **at invoke time** — this is public
information and is deliberately NOT pre-built into a venue database (moat
policy: public information is fetched dynamically, not accumulated as a
substitute for genuine organizational knowledge — see the Web検索失敗時
design decision in docs/02_design.md).

Backend confirmed at MR-B time (2026-07, this session): the AgentCore 1.0.0
wheel's `shared.tools.web_search.web_search` is a **placeholder** —

    @tool
    def web_search(query: str) -> str:
        return f"[Placeholder] Web search results for: {query}"

It always returns a fixed string and is not connected to any real search
backend. There is no other real web-search service anywhere in `shared.*` or
`framework.*` at this SDK version.

Design response (per docs/02_design.md §Web検索失敗時 fallback + Usability §5):
this service is built with an explicit backend injection seam so a real
backend (SerpAPI, Bing, internal search gateway, etc.) can be wired in at
STG/production time via config, WITHOUT touching node code. Until a real
backend is configured, the default backend detects the framework placeholder
shape and treats it as "unavailable", which correctly triggers the
already-designed KB-fallback path in `web_search` node (quality_notes
annotation, `web_search_ok=False`) rather than silently trusting placeholder
text as real market data.
"""

from __future__ import annotations

from typing import Protocol


class WebSearchBackend(Protocol):
    def search(self, query: str) -> str: ...


class PlaceholderDetectedBackend:
    """Default backend: wraps `shared.tools.web_search` and detects its
    known placeholder response shape, surfacing it as "unavailable" instead
    of trusting it as real data.

    STG/production wiring: replace with a real backend (see
    docs/07_operation_guide.md) — inject via `WebSearchService(backend=...)`.
    """

    _PLACEHOLDER_PREFIX = "[Placeholder] Web search results for:"

    def __init__(self) -> None:
        from shared.tools.web_search import web_search as _tool

        self._tool = _tool

    def search(self, query: str) -> str:
        result = self._tool.invoke({"query": query})
        if isinstance(result, str) and result.startswith(self._PLACEHOLDER_PREFIX):
            raise WebSearchUnavailableError(
                "shared.tools.web_search backend is the framework placeholder "
                "(not a real search integration). Configure a real backend "
                "in config/config.yaml / server.py wiring — see "
                "docs/07_operation_guide.md 'Web検索バックエンド設定'."
            )
        return result


class WebSearchUnavailableError(Exception):
    """Raised by a backend when no real search integration is configured/reachable."""


class WebSearchService:
    """Retry-budgeted web search with a swappable backend (injection seam).

    Nodes call `search_destination_options()`; on repeated failure or the
    placeholder-detected condition, callers should treat this as
    `web_search_ok=False` and fall back to past-trip KB data (design decision,
    docs/02_design.md).
    """

    def __init__(self, backend: WebSearchBackend | None = None, *, max_retries: int = 2,
                 timeout_seconds: int = 10) -> None:
        self._backend = backend or PlaceholderDetectedBackend()
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds

    def search_destination_options(self, *, destination_hint: str, season: str,
                                    budget_band: str) -> dict:
        """Returns {"ok": bool, "results": [...], "error": str|None}."""
        query = f"{destination_hint} {season} corporate group trip lodging transport cost {budget_band}"
        last_error: str | None = None
        attempts = max(1, self._max_retries + 1)
        for _attempt in range(attempts):
            try:
                raw = self._backend.search(query)
                return {"ok": True, "results": [{"query": query, "raw": raw}], "error": None}
            except WebSearchUnavailableError as e:
                last_error = str(e)
                break  # placeholder/unavailable is not transient — retrying will not help
            except Exception as e:  # noqa: BLE001 — any backend failure degrades to fallback
                last_error = str(e)
                continue
        return {"ok": False, "results": [], "error": last_error}
