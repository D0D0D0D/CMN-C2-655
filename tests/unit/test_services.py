# CMN-C2-655 — Unit Tests: services layer
# (policy_service / trip_records_service / trip_kb_service / web_search_service /
#  llm_client)

from src.services.llm_client import _parse_json_content
from src.services.policy_service import PolicyService
from src.services.trip_kb_service import TripKBService
from src.services.trip_records_service import TripRecordsService
from src.services.web_search_service import PlaceholderDetectedBackend, WebSearchService, WebSearchUnavailableError


class _FakeDB:
    def __init__(self):
        self.rows = {}
        self.executed = []

    def fetch_one(self, query, params=None):
        key = (params or {}).get("org_unit") or (params or {}).get("plan_id")
        return self.rows.get(key)

    def fetch_many(self, query, params=None):
        return []

    def execute(self, query, params=None):
        self.executed.append(params)


class _FakeVectorStore:
    def __init__(self):
        self.upserted = []

    def search(self, query, top_k=10):
        return [{"metadata": {"destination_area": "Hakone", "satisfaction_score": 4.0}}]

    def upsert(self, documents):
        self.upserted.extend(documents)

    def delete(self, ids):
        pass


class TestPolicyService:
    def test_upsert_then_check(self):
        db = _FakeDB()
        service = PolicyService(db)
        result = service.upsert_policy(org_unit="default", version="v1",
                                        effective_date="2026-01-01",
                                        clauses=[{"clause_no": "1", "text": "x",
                                                  "constraint_type": "per_person_cap",
                                                  "limit_value": 50000}])
        assert result["action"] == "inserted"
        assert result["clause_count"] == 1

    def test_check_constraints_no_policy_unverified(self):
        db = _FakeDB()
        service = PolicyService(db)
        result = service.check_constraints(org_unit="nonexistent", party_size=5, budget_band="standard")
        assert result["status"] == "unverified"


class TestTripRecordsService:
    def test_insert_and_update(self):
        db = _FakeDB()
        service = TripRecordsService(db)
        plan_id = service.insert_plan_record(destination_area="Hakone", season="spring",
                                              party_size=5, budget_band="standard", lodging="Ryokan")
        db.rows[plan_id] = {"plan_id": plan_id}
        result = service.update_feedback(plan_id=plan_id, actual_cost=30000,
                                          satisfaction_score=4.5, complaint_flags=[],
                                          organizer_edited=False)
        assert result["updated"] is True

    def test_update_unknown_plan_id(self):
        db = _FakeDB()
        service = TripRecordsService(db)
        result = service.update_feedback(plan_id="nonexistent", actual_cost=None,
                                          satisfaction_score=None, complaint_flags=[],
                                          organizer_edited=False)
        assert result["updated"] is False


class TestTripKBService:
    def test_upsert_and_search(self):
        vs = _FakeVectorStore()
        service = TripKBService(vs)
        service.upsert_trip_record(plan_id="p1", destination_area="Hakone", season="spring",
                                    party_size=5, budget_band="standard", summary_text="great trip")
        results = service.find_similar_trips(destination_hint="Hakone", season="spring",
                                              party_size=5, budget_band="standard")
        assert len(results) == 1


class TestWebSearchService:
    def test_placeholder_detection(self):
        backend = PlaceholderDetectedBackend()
        try:
            backend.search("Hakone spring")
            raised = False
        except WebSearchUnavailableError:
            raised = True
        assert raised

    def test_service_falls_back_on_unavailable(self):
        class _Backend:
            def search(self, query):
                raise WebSearchUnavailableError("no backend")
        service = WebSearchService(backend=_Backend())
        result = service.search_destination_options(destination_hint="Hakone", season="spring",
                                                      budget_band="standard")
        assert result["ok"] is False


class TestLLMClientJSONParsing:
    def test_parse_json_content_valid(self):
        result = _parse_json_content('{"a": 1}', fallback={})
        assert result == {"a": 1}

    def test_parse_json_content_malformed_falls_back(self):
        result = _parse_json_content("not json", fallback={"records": []})
        assert result == {"records": []}
