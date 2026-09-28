"""AgentCore Platform v1.0 — CMN-C2-655 IngestNormalizeNode.

docs/02_design.md §Node Configuration — 内側: `ingest_normalize`.

Normalizes uploaded travel-policy text / past-trip CSV-Excel rows via
LLM column mapping. Participant personal-info columns
(name / employee ID / contact) are discarded BEFORE any downstream storage —
this is the primary C-01 enforcement point for the ingest path.
"""

from __future__ import annotations

import re
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.llm_client import TripPlannerLLMClient

# Column-name heuristics for personal-info columns to discard pre-storage.
_PERSONAL_COLUMN_PATTERN = re.compile(
    r"name|氏名|姓名|社員番号|employee_?id|email|mail|phone|tel|電話|連絡先",
    re.IGNORECASE,
)


class IngestNormalizeNode(FunctionNode):
    """Normalizes ingest rows and discards personal-info columns."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm_client: TripPlannerLLMClient | None = None) -> None:
        super().__init__()
        self._llm_client = llm_client

    def execute(self, state: dict) -> dict:
        params = state.get("request_params", {})
        rows = params.get("rows", [])
        ingest_kind = params.get("ingest_kind", "unknown")

        rows_stripped, dropped_columns = self._strip_personal_columns(rows)

        if self._llm_client is not None and rows_stripped:
            normalized = self._llm_client.normalize_records(rows_stripped, record_kind=ingest_kind)
        else:
            normalized = {"records": rows_stripped, "column_mapping": {}, "dropped_columns": []}

        dropped_columns = sorted(set(dropped_columns) | set(normalized.get("dropped_columns", [])))

        emit_trace_event("ingest_normalized", {
            "ingest_kind": ingest_kind,
            "row_count": len(rows_stripped),
            "dropped_column_count": len(dropped_columns),
        }, state)

        return {
            "normalized_records": normalized.get("records", []),
            "ingest_summary": {
                "ingest_kind": ingest_kind,
                "column_mapping": normalized.get("column_mapping", {}),
                "dropped_columns": dropped_columns,
                "row_count": len(normalized.get("records", [])),
            },
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _strip_personal_columns(rows: list[dict]) -> tuple[list[dict], list[str]]:
        """Discards personal-info columns before any downstream use (C-01)."""
        dropped: set[str] = set()
        cleaned: list[dict] = []
        for row in rows:
            clean_row = {}
            for key, value in row.items():
                if _PERSONAL_COLUMN_PATTERN.search(str(key)):
                    dropped.add(key)
                    continue
                clean_row[key] = value
            cleaned.append(clean_row)
        return cleaned, sorted(dropped)

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
