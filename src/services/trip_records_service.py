"""AgentCore Platform v1.0 — CMN-C2-655 trip_records DB service.

`trip_records` holds only non-personal trip-outcome fields (docs/02_design.md
§Services層 schema): plan_id PK / destination_area / season / party_size /
budget_band / lodging / actual_cost / satisfaction_score / complaint_flags /
organizer_edited / created_at. No name, contact, or care-need columns (C-01).
"""

from __future__ import annotations

from datetime import datetime, timezone

from shared.services.database.base_db import BaseDatabase

from src.services.plan_identifier import new_plan_id


class TripRecordsService:
    def __init__(self, db: BaseDatabase) -> None:
        self._db = db

    def insert_plan_record(
        self,
        *,
        destination_area: str,
        season: str,
        party_size: int,
        budget_band: str,
        lodging: str,
        actual_cost: float | None = None,
        satisfaction_score: float | None = None,
        complaint_flags: list[str] | None = None,
        organizer_edited: bool | None = None,
    ) -> str:
        """Insert a new trip record at plan time; returns the generated plan_id."""
        plan_id = new_plan_id()
        self._db.execute(
            "INSERT INTO trip_records (plan_id, destination_area, season, party_size, "
            "budget_band, lodging, actual_cost, satisfaction_score, complaint_flags, "
            "organizer_edited, created_at) VALUES (:plan_id, :destination_area, :season, "
            ":party_size, :budget_band, :lodging, :actual_cost, :satisfaction_score, "
            ":complaint_flags, :organizer_edited, :created_at)",
            {
                "plan_id": plan_id,
                "destination_area": destination_area,
                "season": season,
                "party_size": party_size,
                "budget_band": budget_band,
                "lodging": lodging,
                "actual_cost": actual_cost,
                "satisfaction_score": satisfaction_score,
                "complaint_flags": complaint_flags or [],
                "organizer_edited": organizer_edited,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return plan_id

    def get_plan_record(self, plan_id: str) -> dict | None:
        """Return one persisted non-personal trip outcome by its public id."""
        return self._db.fetch_one(
            "SELECT plan_id, destination_area, season, party_size, budget_band, lodging, "
            "actual_cost, satisfaction_score, complaint_flags, organizer_edited, created_at "
            "FROM trip_records WHERE plan_id = :plan_id",
            {"plan_id": plan_id},
        )

    def update_feedback(self, *, plan_id: str, actual_cost: float | None,
                         satisfaction_score: float | None, complaint_flags: list[str],
                         organizer_edited: bool) -> dict:
        """UPDATE with realized cost / satisfaction / complaint codes (no free text, no names)."""
        existing = self._db.fetch_one(
            "SELECT plan_id FROM trip_records WHERE plan_id = :plan_id", {"plan_id": plan_id},
        )
        if not existing:
            return {"updated": False, "reason": "plan_id not found"}
        self._db.execute(
            "UPDATE trip_records SET actual_cost = :actual_cost, "
            "satisfaction_score = :satisfaction_score, complaint_flags = :complaint_flags, "
            "organizer_edited = :organizer_edited WHERE plan_id = :plan_id",
            {
                "plan_id": plan_id,
                "actual_cost": actual_cost,
                "satisfaction_score": satisfaction_score,
                "complaint_flags": complaint_flags,
                "organizer_edited": organizer_edited,
            },
        )
        return {"updated": True}
