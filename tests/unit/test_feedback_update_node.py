# CMN-C2-655 — Unit Tests: FeedbackUpdateNode

from framework.schemas.agent_status import AgentStatus
from src.nodes.feedback_update_node import FeedbackUpdateNode
from src.services.trip_records_service import TripRecordsService


class _FakeDB:
    def __init__(self):
        self.rows = {"p-1": {"plan_id": "p-1"}}
        self.updates = []

    def fetch_one(self, query, params=None):
        return self.rows.get(params.get("plan_id"))

    def fetch_many(self, query, params=None):
        return []

    def execute(self, query, params=None):
        self.updates.append(params)


class TestFeedbackUpdateNodeNotWired:
    def test_no_service_degrades_gracefully(self):
        node = FeedbackUpdateNode()
        state = {"request_params": {"plan_id": "p-1", "complaint_text": "ok"},
                 "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
                 "node_history": [], "error_log": []}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["feedback_result"]["updated"] is False


class TestFeedbackUpdateNodeWired:
    def test_updates_and_categorizes_flags(self):
        db = _FakeDB()
        service = TripRecordsService(db)
        node = FeedbackUpdateNode(trip_records_service=service)
        state = {
            "request_params": {"plan_id": "p-1", "actual_cost": 32000, "satisfaction_score": 2.0,
                                "complaint_text": "料理が遅くて残念でした"},
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["feedback_result"]["updated"] is True
        assert "delay" in result["feedback_result"]["complaint_flags"] or \
               "food_quality" in result["feedback_result"]["complaint_flags"]

    def test_complaint_text_identifier_sanitized_before_kb_write(self):
        db = _FakeDB()
        service = TripRecordsService(db)

        captured = {}

        class _FakeKB:
            def upsert_trip_record(self, **kwargs):
                captured.update(kwargs)

        node = FeedbackUpdateNode(trip_records_service=service, trip_kb_service=_FakeKB())
        state = {
            "request_params": {"plan_id": "p-1",
                                "complaint_text": "田中太郎さんから、料理が遅いとの声がありました。"},
            "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
            "node_history": [], "error_log": [],
        }
        node.execute(state)
        assert "田中太郎" not in captured.get("summary_text", "")

    def test_unknown_plan_id_returns_not_updated(self):
        db = _FakeDB()
        service = TripRecordsService(db)
        node = FeedbackUpdateNode(trip_records_service=service)
        state = {"request_params": {"plan_id": "nonexistent"},
                 "caller_trust_level": "VERIFIED_EXTERNAL", "status": "pending",
                 "node_history": [], "error_log": []}
        result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["feedback_result"]["updated"] is False
