# CMN-C2-655 — Unit Tests: IngestStoreNode

from framework.schemas.agent_status import AgentStatus
from src.nodes.ingest_store_node import IngestStoreNode
from src.services.policy_service import PolicyService
from src.services.trip_records_service import TripRecordsService


class _FakeDB:
    def __init__(self):
        self.executed = []

    def fetch_one(self, query, params=None):
        return None

    def fetch_many(self, query, params=None):
        return []

    def execute(self, query, params=None):
        self.executed.append(params)


class TestIngestStoreNodePolicy:
    def test_stores_policy_clauses(self):
        db = _FakeDB()
        service = PolicyService(db)
        node = IngestStoreNode(policy_service=service)
        state = {
            "request_params": {"ingest_kind": "policy", "org_unit": "default",
                                "version": "v1", "effective_date": "2026-01-01"},
            "normalized_records": [{"clause_no": "1", "text": "上限5万円",
                                     "constraint_type": "per_person_cap", "limit_value": 50000}],
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["ingest_result"]["ingested_count"] == 1

    def test_not_wired_skips_gracefully(self):
        node = IngestStoreNode()
        state = {
            "request_params": {"ingest_kind": "policy"},
            "normalized_records": [{"clause_no": "1", "text": "x"}],
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["ingest_result"]["ingested_count"] == 0


class TestIngestStoreNodeUnknownKind:
    def test_unknown_ingest_kind_skips_all(self):
        node = IngestStoreNode()
        state = {
            "request_params": {"ingest_kind": "bogus"},
            "normalized_records": [{"a": 1}],
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = node.execute(state)
        assert result["ingest_result"]["skipped_count"] == 1


class TestIngestStoreNodeTripRecords:
    def test_db_persistence_does_not_depend_on_optional_kb(self):
        db = _FakeDB()
        node = IngestStoreNode(trip_records_service=TripRecordsService(db))
        state = {
            "request_params": {"ingest_kind": "trip_records"},
            "normalized_records": [{
                "destination_area": "Hakone",
                "season": "spring",
                "party_size": 8,
                "budget_band": "standard",
                "lodging": "Ryokan",
            }],
            "caller_trust_level": "VERIFIED_EXTERNAL",
            "status": "pending",
            "node_history": [],
            "error_log": [],
        }

        result = node.execute(state)["ingest_result"]

        assert result["ingested_count"] == 1
        assert result["kb_indexed_count"] == 0
        assert result["kb_skipped_count"] == 1
        assert len(db.executed) == 1
