# PB-3: L1 -> External Service Verification
# Template connects to an external service (DB / vector store / web search)
# via the L1 framework's injected abstractions (BaseDatabase / BaseVectorStore),
# not a hand-rolled client, and real data retrieval is exercised (fake
# implementations standing in for the actual Postgres/pgvector backends,
# which are unconfigured placeholders in the current SDK — see
# src/services/*.py module docstrings).

from shared.services.database.base_db import BaseDatabase
from shared.services.vector_store.base_vector_store import BaseVectorStore

from src.services.policy_service import PolicyService
from src.services.trip_kb_service import TripKBService
from src.services.trip_records_service import TripRecordsService


class _FakeDatabase(BaseDatabase):
    """Minimal in-memory stand-in conforming to the real BaseDatabase contract."""

    def __init__(self):
        self._policy_rows = {}

    def fetch_one(self, query, params=None):
        params = params or {}
        if "org_unit" in params and "version" in params:
            return self._policy_rows.get((params["org_unit"], params["version"]))
        if "org_unit" in params:
            matches = [v for (org, _), v in self._policy_rows.items() if org == params["org_unit"]]
            return matches[0] if matches else None
        return None

    def fetch_many(self, query, params=None):
        return list(self._policy_rows.values())

    def execute(self, query, params=None):
        params = params or {}
        if "org_unit" in params and "version" in params and "clauses" in params:
            self._policy_rows[(params["org_unit"], params["version"])] = {
                "version": params["version"], "clauses": params["clauses"],
            }


class _FakeVectorStore(BaseVectorStore):
    def __init__(self):
        self._docs = []

    def search(self, query, top_k=10):
        return self._docs[:top_k]

    def upsert(self, documents):
        self._docs.extend(documents)

    def delete(self, ids):
        self._docs = [d for d in self._docs if d.get("id") not in ids]


class TestPBExternalServicePolicyDB:
    def test_policy_service_real_roundtrip_via_base_database_contract(self):
        db = _FakeDatabase()
        service = PolicyService(db)
        assert isinstance(db, BaseDatabase)

        service.upsert_policy(org_unit="default", version="v1", effective_date="2026-01-01",
                                clauses=[{"clause_no": "1", "text": "上限5万円",
                                          "constraint_type": "per_person_cap", "limit_value": 50000}])
        result = service.check_constraints(org_unit="default", party_size=5, budget_band="standard")
        assert result["status"] == "verified"
        assert result["constraints"][0]["limit_value"] == 50000


class TestPBExternalServiceTripKB:
    def test_trip_kb_service_real_roundtrip_via_base_vector_store_contract(self):
        vs = _FakeVectorStore()
        service = TripKBService(vs)
        assert isinstance(vs, BaseVectorStore)

        service.upsert_trip_record(plan_id="p1", destination_area="Hakone", season="spring",
                                    party_size=6, budget_band="standard", summary_text="great trip",
                                    satisfaction_score=4.5, complaint_flags=[])
        results = service.find_similar_trips(destination_hint="Hakone", season="spring",
                                              party_size=6, budget_band="standard")
        assert len(results) == 1
        assert results[0]["metadata"]["destination_area"] == "Hakone"


class TestPBExternalServiceTripRecords:
    def test_trip_records_service_real_roundtrip(self):
        db = _FakeDatabase()

        # extend fake DB minimally for trip_records semantics used by this service
        rows = {}
        db.execute_orig = db.execute

        def execute(query, params=None):
            params = params or {}
            if "plan_id" in params and "destination_area" in params:
                rows[params["plan_id"]] = dict(params)
            elif "plan_id" in params:
                rows.setdefault(params["plan_id"], {}).update(params)

        def fetch_one(query, params=None):
            params = params or {}
            return rows.get(params.get("plan_id"))

        db.execute = execute
        db.fetch_one = fetch_one

        service = TripRecordsService(db)
        plan_id = service.insert_plan_record(destination_area="Hakone", season="spring",
                                              party_size=6, budget_band="standard", lodging="Ryokan")
        result = service.update_feedback(plan_id=plan_id, actual_cost=32000,
                                          satisfaction_score=4.2, complaint_flags=[],
                                          organizer_edited=False)
        assert result["updated"] is True
