# CMN-C2-655 — Unit Tests: IngestNormalizeNode
# C-01: personal-info columns must be discarded before any downstream use.

from framework.schemas.agent_status import AgentStatus
from src.nodes.ingest_normalize_node import IngestNormalizeNode


class TestIngestNormalizeNodePersonalColumnStrip:
    def setup_method(self):
        self.node = IngestNormalizeNode()

    def test_discards_name_and_contact_columns(self):
        state = {
            "request_params": {
                "ingest_kind": "trip_records",
                "rows": [{"employee_name": "田中太郎", "email": "tanaka@example.com",
                           "destination_area": "Hakone", "satisfaction_score": 4.5}],
            },
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        record = result["normalized_records"][0]
        assert "employee_name" not in record
        assert "email" not in record
        assert record["destination_area"] == "Hakone"
        assert "employee_name" in result["ingest_summary"]["dropped_columns"]

    def test_no_llm_client_passthrough(self):
        state = {
            "request_params": {"ingest_kind": "policy", "rows": [{"clause_no": "1", "text": "x"}]},
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = self.node.execute(state)
        assert len(result["normalized_records"]) == 1
