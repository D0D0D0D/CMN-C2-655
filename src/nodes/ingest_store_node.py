"""AgentCore Platform v1.0 — CMN-C2-655 IngestStoreNode.

docs/02_design.md §Node Configuration — 内側: `ingest_store`.

Persists normalized records: policy rows -> travel_policy (version-managed
UPSERT via PolicyService); trip-record rows -> trip_records DB + past-trip KB
(vector) via TripRecordsService / TripKBService.
"""

from __future__ import annotations

from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.policy_service import PolicyService
from src.services.trip_kb_service import TripKBService
from src.services.trip_records_service import TripRecordsService


class IngestStoreNode(FunctionNode):
    """Persists normalized ingest records into policy DB or trip KB/DB."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, policy_service: PolicyService | None = None,
                 trip_records_service: TripRecordsService | None = None,
                 trip_kb_service: TripKBService | None = None) -> None:
        super().__init__()
        self._policy_service = policy_service
        self._trip_records_service = trip_records_service
        self._trip_kb_service = trip_kb_service

    def execute(self, state: dict) -> dict:
        params = state.get("request_params", {})
        ingest_kind = params.get("ingest_kind", "unknown")
        records = state.get("normalized_records", [])

        if ingest_kind == "policy":
            result = self._store_policy(params, records)
        elif ingest_kind == "trip_records":
            result = self._store_trip_records(params, records)
        else:
            result = {"ingested_count": 0, "skipped_count": len(records),
                      "reason": f"unknown ingest_kind '{ingest_kind}'"}

        emit_trace_event("ingest_stored", {
            "ingest_kind": ingest_kind,
            "ingested_count": result.get("ingested_count", 0),
            "skipped_count": result.get("skipped_count", 0),
        }, state)

        return {"ingest_result": result, "status": AgentStatus.SUCCESS.value}

    def _store_policy(self, params: dict, records: list[dict]) -> dict:
        if self._policy_service is None:
            return {"ingested_count": 0, "skipped_count": len(records),
                     "reason": "policy_service not wired"}
        org_unit = params.get("org_unit", "default")
        version = params.get("version", "")
        effective_date = params.get("effective_date", "")
        clauses = [
            {"clause_no": r.get("clause_no", str(i + 1)), "text": r.get("text", ""),
             "constraint_type": r.get("constraint_type", ""), "limit_value": r.get("limit_value")}
            for i, r in enumerate(records)
        ]
        result = self._policy_service.upsert_policy(
            org_unit=org_unit, version=version, effective_date=effective_date, clauses=clauses,
        )
        return {"ingested_count": result["clause_count"], "skipped_count": 0,
                "policy_version": version, "action": result["action"]}

    def _store_trip_records(self, params: dict, records: list[dict]) -> dict:
        if self._trip_records_service is None:
            return {"ingested_count": 0, "skipped_count": len(records),
                     "reason": "trip_records_service not wired"}
        ingested = 0
        skipped = 0
        kb_indexed = 0
        for row in records:
            try:
                party_size = int(row.get("party_size") or 0)
                plan_id = self._trip_records_service.insert_plan_record(
                    destination_area=row.get("destination_area", ""),
                    season=row.get("season", ""),
                    party_size=party_size,
                    budget_band=row.get("budget_band", ""),
                    lodging=row.get("lodging", ""),
                    actual_cost=row.get("actual_cost"),
                    satisfaction_score=row.get("satisfaction_score"),
                    complaint_flags=row.get("complaint_flags"),
                    organizer_edited=row.get("organizer_edited"),
                )
            except (ValueError, TypeError, KeyError):
                skipped += 1
                continue

            ingested += 1
            if self._trip_kb_service is None:
                continue
            try:
                self._trip_kb_service.upsert_trip_record(
                    plan_id=plan_id,
                    destination_area=row.get("destination_area", ""),
                    season=row.get("season", ""),
                    party_size=party_size,
                    budget_band=row.get("budget_band", ""),
                    summary_text=row.get("summary_text", ""),
                    satisfaction_score=row.get("satisfaction_score"),
                    complaint_flags=row.get("complaint_flags"),
                )
                kb_indexed += 1
            except (ValueError, TypeError, KeyError, NotImplementedError):
                # DB is the source of record. Optional indexing failure must
                # not misreport or roll back an already committed DB insert.
                continue
        return {
            "ingested_count": ingested,
            "skipped_count": skipped,
            "kb_indexed_count": kb_indexed,
            "kb_skipped_count": ingested - kb_indexed,
        }

    def _extra_security_gate_input(self, state: dict) -> dict:
        return state
