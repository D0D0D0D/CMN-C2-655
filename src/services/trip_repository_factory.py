"""Construction seam for the corporate-trip persistence repositories."""

from __future__ import annotations

from dataclasses import dataclass, field

from shared.services.database.base_db import BaseDatabase

from src.services.sqlite_database import SQLiteDatabase


@dataclass(frozen=True)
class TripRepositories:
    """Database ports consumed by the policy and trip-record repositories."""

    policy_database: BaseDatabase
    trip_records_database: BaseDatabase
    _owned_database: BaseDatabase | None = field(default=None, repr=False)

    def close(self) -> None:
        """Close each distinct owned backend at most once."""

        if self._owned_database is not None:
            close = getattr(self._owned_database, "close", None)
            if close is not None:
                close()


def create_trip_repositories(
    sqlite_path: str,
    *,
    database: BaseDatabase | None = None,
) -> TripRepositories:
    """Create all write/read repositories from one explicitly selected backend.

    The optional port keeps the application contract backend-independent. The
    default is this agent's working SQLite implementation; replacing it later
    happens here rather than in a read-only retrieval adapter.
    """

    owned_database = SQLiteDatabase(sqlite_path) if database is None else None
    selected = database if database is not None else owned_database
    assert selected is not None
    return TripRepositories(
        policy_database=selected,
        trip_records_database=selected,
        _owned_database=owned_database,
    )
