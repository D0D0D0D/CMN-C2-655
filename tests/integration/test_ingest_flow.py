# CMN-C2-655 — Integration Tests: /ingest mode
# C-01: personal-info columns must never reach travel_policy/trip_records/KB.

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import CorporateGroupTripPlannerGraph


def _build_agent():
    agent = CorporateGroupTripPlannerGraph(config={"memory_enabled": True,
                                                     "hitl": {"enabled": True, "max_hitl": 24}})
    agent.compile(checkpointer=InMemorySaver())
    return agent


class TestIngestPolicy:
    def test_ingest_policy_rows_succeeds(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {
            "mode": "ingest",
            "ingest_payload": {
                "ingest_kind": "policy",
                "rows": [{"clause_no": "1", "text": "一人当たり上限5万円",
                          "constraint_type": "per_person_cap", "limit_value": 50000}],
                "org_unit": "default", "version": "v1", "effective_date": "2026-01-01",
            },
        }
        result = agent.invoke("", ctx=ctx, input_context=input_context)
        assert result["status"] == "success"


class TestIngestRowLimit:
    def test_exceeding_500_rows_errors(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {
            "mode": "ingest",
            "ingest_payload": {"ingest_kind": "trip_records", "rows": [{"a": 1}] * 501},
        }
        result = agent.invoke("", ctx=ctx, input_context=input_context)
        assert result["status"] == "error"


class TestIngestPersonalColumnDiscard:
    def test_personal_columns_never_reach_output(self):
        agent = _build_agent()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {
            "mode": "ingest",
            "ingest_payload": {
                "ingest_kind": "trip_records",
                "rows": [{"employee_name": "田中太郎", "email": "tanaka@example.com",
                           "destination_area": "Hakone", "satisfaction_score": 4.5}],
                "org_unit": "default",
            },
        }
        result = agent.invoke("", ctx=ctx, input_context=input_context)
        assert result["status"] == "success"
        import json
        dump = json.dumps(result, default=str, ensure_ascii=False)
        assert "田中太郎" not in dump
        assert "tanaka@example.com" not in dump
