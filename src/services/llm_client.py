"""AgentCore Platform v1.0 — CMN-C2-655 LLM client injection seam.

Wraps `shared.services.llm.base_llm.BaseLLM` (constructor-injected — tests
supply a fake) for the three LLM-backed operations this template needs:
ingest column-mapping normalization, plan synthesis, and HITL resume-response
interpretation. Level 0 (the platform SDK) is never imported here — only the
BaseLLM abstract contract from shared/.
"""

from __future__ import annotations

import json

from shared.services.llm.base_llm import BaseLLM


class TripPlannerLLMClient:
    """Thin structured-prompt wrapper around an injected BaseLLM."""

    def __init__(self, llm: BaseLLM, *, model: str = "claude-sonnet", temperature: float = 0.2,
                 max_tokens: int = 4000) -> None:
        self._llm = llm
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

    def normalize_records(self, raw_rows: list[dict], *, record_kind: str) -> dict:
        """Column-map an arbitrary CSV/Excel row shape into the canonical schema.

        Returns {"records": [...], "column_mapping": {...}, "dropped_columns": [...]}.
        The caller (ingest_normalize node) is responsible for stripping personal
        fields — this method does not decide what is personal.
        """
        prompt = (
            f"Normalize the following {record_kind} rows into a canonical schema. "
            "Return strict JSON with keys: records (list of normalized row dicts), "
            "column_mapping (dict of source->canonical column names), "
            "dropped_columns (list of source column names that could not be mapped).\n\n"
            f"Rows:\n{json.dumps(raw_rows, ensure_ascii=False, default=str)}"
        )
        response = self._llm.complete([{"role": "user", "content": prompt}])
        return _parse_json_content(response.get("content", ""), fallback={
            "records": raw_rows, "column_mapping": {}, "dropped_columns": [],
        })

    def synthesize_plans(self, *, request_params: dict, policy_constraints: list,
                          policy_citations: list, ranked_candidates: list,
                          care_requirements: list, plans_count: int = 3,
                          refine_feedback: str | None = None) -> list[dict]:
        """Synthesize N plan candidates. If refine_feedback is given, reweight/adjust."""
        prompt = (
            f"Produce {plans_count} distinct corporate group trip plans as strict JSON "
            '(top-level key "plans", a list of objects each with keys: '
            "destination_area, itinerary (list of day strings), lodging, "
            "transport, estimated_cost_per_person, rationale, care_accommodations).\n"
            f"Request parameters: {json.dumps(request_params, ensure_ascii=False, default=str)}\n"
            f"Policy constraints: {json.dumps(policy_constraints, ensure_ascii=False, default=str)}\n"
            f"Policy citations: {json.dumps(policy_citations, ensure_ascii=False, default=str)}\n"
            f"Ranked candidates (from past-trip KB / web search): "
            f"{json.dumps(ranked_candidates, ensure_ascii=False, default=str)}\n"
            f"Care requirements (anonymized aggregate — category x count only): "
            f"{json.dumps(care_requirements, ensure_ascii=False, default=str)}\n"
        )
        if refine_feedback:
            prompt += f"\nHuman refine feedback to incorporate: {refine_feedback}\n"
        response = self._llm.complete([{"role": "user", "content": prompt}])
        parsed = _parse_json_content(response.get("content", ""), fallback={"plans": []})
        return parsed.get("plans", [])

    def categorize_complaint(self, sanitized_text: str) -> list[str]:
        """Categorizes an (already-sanitized) complaint into complaint_flags.

        Returns a list of canonical flag codes (e.g. "delay", "cleanliness",
        "cost_overrun", "food_quality", "cancellation", or others the LLM
        infers). Caller must pass text that has ALREADY had direct
        identifiers removed — this method never sees raw PII.
        """
        prompt = (
            "Categorize the following corporate-trip complaint into canonical "
            "flag codes (snake_case, e.g. delay, cleanliness, cost_overrun, "
            "food_quality, cancellation — infer additional codes if none fit). "
            'Return strict JSON {"flags": ["code1", "code2", ...]}.\n\n'
            f"Complaint: {sanitized_text}"
        )
        response = self._llm.complete([{"role": "user", "content": prompt}])
        parsed = _parse_json_content(response.get("content", ""), fallback={"flags": []})
        flags = parsed.get("flags", [])
        return sorted(set(flags)) if isinstance(flags, list) else []

    def interpret_refine_response(self, sanitized_text: str) -> dict:
        """Interpret a (already-sanitized) human refine response into a directive.

        Returns {"directive": "approve"|"adjust"|"new_search", "notes": str}.
        Caller must pass text that has ALREADY had direct identifiers removed —
        this method never sees raw PII.
        """
        prompt = (
            "Classify the following corporate-trip-organizer response into exactly one "
            'of three directives: "approve" (accept a plan as-is), "adjust" '
            "(same destination/candidates, change weighting/conditions and re-synthesize), "
            'or "new_search" (organizer explicitly asked for a different destination or '
            "search axis). Return strict JSON {\"directive\": ..., \"notes\": \"short "
            f"summary of the requested change\"}}.\n\nResponse: {sanitized_text}"
        )
        response = self._llm.complete([{"role": "user", "content": prompt}])
        return _parse_json_content(response.get("content", ""), fallback={
            "directive": "adjust", "notes": sanitized_text[:200],
        })


def _parse_json_content(content: str, *, fallback: dict) -> dict:
    """Best-effort JSON parse of an LLM completion; falls back on malformed output.

    LLM outputs are not guaranteed valid JSON — never let a malformed response
    crash a node; degrade to `fallback` and let quality_notes reflect it.
    """
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    return fallback
