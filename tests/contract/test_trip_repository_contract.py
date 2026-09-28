"""Backend-independent acceptance contract for corporate-trip persistence.

Backend construction is confined to the fixture. Assertions use only the
domain services' public methods; SQL syntax, LIKE behavior, tokenization, and
backend internals are deliberately outside this contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from shared.services.database.base_db import BaseDatabase
from src.services.policy_service import PolicyService
from src.services.trip_records_service import TripRecordsService
from src.services.trip_repository_factory import create_trip_repositories


class _RecordingDatabase(BaseDatabase):
    """Records port calls and parameters without interpreting query text."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.close_calls = 0

    def fetch_one(self, _query: str, params: dict | None = None) -> dict | None:
        self.calls.append(("fetch_one", dict(params or {})))
        return None

    def fetch_many(self, _query: str, params: dict | None = None) -> list[dict]:
        self.calls.append(("fetch_many", dict(params or {})))
        return []

    def execute(self, _query: str, params: dict | None = None) -> int:
        self.calls.append(("execute", dict(params or {})))
        return 1

    def close(self) -> None:
        self.close_calls += 1


@pytest.fixture()
def repositories(tmp_path):
    repositories = create_trip_repositories(str(tmp_path / "corporate-trips.sqlite3"))
    yield repositories
    repositories.close()


def test_factory_prefers_injected_backend_for_both_repositories(tmp_path):
    injected = _RecordingDatabase()
    ignored_sqlite_path = tmp_path / "missing-parent" / "must-not-be-created.sqlite3"

    repositories = create_trip_repositories(str(ignored_sqlite_path), database=injected)
    policies = PolicyService(repositories.policy_database)
    trips = TripRecordsService(repositories.trip_records_database)

    policies.upsert_policy(
        org_unit="営業本部",
        version="v1",
        effective_date="2026-01-01",
        clauses=[{"clause_no": "1", "constraint_type": "cap", "limit_value": 100}],
    )
    trips.insert_plan_record(
        destination_area="箱根",
        season="spring",
        party_size=8,
        budget_band="standard",
        lodging="旅館",
    )

    fetch_params = [params for method, params in injected.calls if method == "fetch_one"]
    execute_params = [params for method, params in injected.calls if method == "execute"]
    assert repositories.policy_database is injected
    assert repositories.trip_records_database is injected
    assert repositories._owned_database is None
    assert {"org_unit": "営業本部", "version": "v1"} in fetch_params
    assert any(params.get("clauses") for params in execute_params)
    assert any(params.get("destination_area") == "箱根" for params in execute_params)
    assert not ignored_sqlite_path.exists()

    repositories.close()
    assert injected.close_calls == 0


def test_policy_versions_round_trip_and_latest_effective_version_wins(repositories):
    policies = PolicyService(repositories.policy_database)

    inserted = policies.upsert_policy(
        org_unit="sales",
        version="2026-01",
        effective_date="2026-01-01",
        clauses=[
            {
                "clause_no": "A-1",
                "text": "宿泊費は一人一泊二万円まで",
                "constraint_type": "lodging_cap",
                "limit_value": 20000,
            }
        ],
    )
    policies.upsert_policy(
        org_unit="sales",
        version="2026-04",
        effective_date="2026-04-01",
        clauses=[
            {
                "clause_no": "B-2",
                "text": "宿泊費は一人一泊二万五千円まで",
                "constraint_type": "lodging_cap",
                "limit_value": 25000,
            }
        ],
    )
    updated = policies.upsert_policy(
        org_unit="sales",
        version="2026-04",
        effective_date="2026-04-15",
        clauses=[
            {
                "clause_no": "B-2",
                "text": "改定後の宿泊費上限",
                "constraint_type": "lodging_cap",
                "limit_value": 27000,
            }
        ],
    )

    result = policies.check_constraints(org_unit="sales", party_size=12, budget_band="standard")

    assert inserted["action"] == "inserted"
    assert updated["action"] == "updated"
    assert result["status"] == "verified"
    assert result["constraints"] == [{"constraint_type": "lodging_cap", "limit_value": 27000}]
    assert result["citations"][0]["policy_version"] == "2026-04"


def test_policy_lookup_requires_exact_org_unit_match(repositories):
    policies = PolicyService(repositories.policy_database)
    policy_values = [
        ("営業本部", "2026-01-01", 100),
        ("営業本部第二", "2028-01-01", 200),
        ("営業", "2030-01-01", 50),
    ]
    for org_unit, effective_date, limit_value in policy_values:
        policies.upsert_policy(
            org_unit=org_unit,
            version="scope-v1",
            effective_date=effective_date,
            clauses=[
                {
                    "clause_no": org_unit,
                    "text": f"{org_unit} only",
                    "constraint_type": "scope_marker",
                    "limit_value": limit_value,
                }
            ],
        )

    for org_unit, _effective_date, expected_value in policy_values:
        result = policies.check_constraints(org_unit=org_unit, party_size=1, budget_band="standard")
        assert result["constraints"] == [{"constraint_type": "scope_marker", "limit_value": expected_value}]
        assert result["citations"][0]["clause_no"] == org_unit


def test_policy_upsert_identity_requires_exact_org_unit_and_version(repositories):
    policies = PolicyService(repositories.policy_database)
    policies.upsert_policy(
        org_unit="営業本部第二",
        version="shared-version",
        effective_date="2028-01-01",
        clauses=[
            {
                "clause_no": "second",
                "text": "第二のみ",
                "constraint_type": "scope_marker",
                "limit_value": 200,
            }
        ],
    )

    exact_insert = policies.upsert_policy(
        org_unit="営業本部",
        version="shared-version",
        effective_date="2026-01-01",
        clauses=[
            {
                "clause_no": "headquarters",
                "text": "本部のみ",
                "constraint_type": "scope_marker",
                "limit_value": 100,
            }
        ],
    )

    headquarters = policies.check_constraints(org_unit="営業本部", party_size=1, budget_band="standard")
    second = policies.check_constraints(org_unit="営業本部第二", party_size=1, budget_band="standard")
    assert exact_insert["action"] == "inserted"
    assert headquarters["constraints"][0]["limit_value"] == 100
    assert second["constraints"][0]["limit_value"] == 200


def test_trip_record_and_feedback_round_trip_preserves_domain_types(repositories):
    trips = TripRecordsService(repositories.trip_records_database)

    plan_id = trips.insert_plan_record(
        destination_area="箱根",
        season="spring",
        party_size=18,
        budget_band="standard",
        lodging="旅館",
        actual_cost=475000.0,
        satisfaction_score=4.0,
        complaint_flags=["delay"],
        organizer_edited=False,
    )
    initially_stored = trips.get_plan_record(plan_id)
    update = trips.update_feedback(
        plan_id=plan_id,
        actual_cost=480000.5,
        satisfaction_score=4.25,
        complaint_flags=["delay", "food_quality"],
        organizer_edited=True,
    )
    stored = trips.get_plan_record(plan_id)

    assert initially_stored is not None
    assert initially_stored["actual_cost"] == 475000.0
    assert initially_stored["complaint_flags"] == ["delay"]
    assert initially_stored["organizer_edited"] is False
    assert update == {"updated": True}
    assert stored is not None
    assert stored["destination_area"] == "箱根"
    assert stored["party_size"] == 18
    assert stored["actual_cost"] == 480000.5
    assert stored["satisfaction_score"] == 4.25
    assert stored["complaint_flags"] == ["delay", "food_quality"]
    assert stored["organizer_edited"] is True


def test_unknown_policy_and_plan_have_stable_not_found_behavior(repositories):
    policies = PolicyService(repositories.policy_database)
    trips = TripRecordsService(repositories.trip_records_database)

    assert policies.check_constraints(org_unit="unknown", party_size=1, budget_band="economy") == {
        "status": "unverified",
        "constraints": [],
        "citations": [],
    }
    assert trips.get_plan_record("unknown") is None
    assert trips.update_feedback(
        plan_id="unknown",
        actual_cost=None,
        satisfaction_score=None,
        complaint_flags=[],
        organizer_edited=False,
    ) == {"updated": False, "reason": "plan_id not found"}
